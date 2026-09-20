"""隧道掘进施工剖面服务。

以每方案一条事件日志为核心：
- 实时推进 = 处理传感读数 -> 推演风险 -> 规则引擎 -> 指令仲裁 -> 追加事件；
- 迟到数据 = 按事件时间判定，容忍窗口内追溯修订并标注 retroactive；
- 人工指令与自动保护同窗发生 = 按安全优先级仲裁并记录拒绝原因；
- 多方案 = 从历史稳定节点分叉，active 方案下发现场，whatif 方案只推演；
- 服务重启 = 重放全部事件日志还原状态，未决指令继续参与下一窗口仲裁。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .models import (
    Origin, Ack, ScenarioRole, ReadStatus,
    StrataColumn, ConstructionProfile, Telemetry, RingRisk,
    FactorNode, AnomalyRecord, StateSnapshot, StableNode,
)
from .risk import (
    strata_changed, predict_risk, evaluate_breaches, is_anomaly, is_critical,
)
from .store import EventStore, Event

# 迟到容忍：读数事件时间落后当前环水位线不超过该值时允许追溯重放（ms）
LATE_TOLERANCE_MS = 30 * 60 * 1000
# 连续多少个无异常环即可成为稳定节点
STABLE_RUN_RINGS = 3


@dataclass
class Command:
    """一条待仲裁指令（人工 / 保护 / 自动优化）。"""
    cmd_id: str
    action: str                 # emergency_stop / resume / set_pressure /
                                # set_speed / set_torque / set_grout /
                                # pressure_hold / reset_lockdown
    origin: str
    value: Optional[float] = None
    issued_ms: int = 0
    note: str = ""
    # 仲裁后填充
    ack: Optional[str] = None
    reason: str = ""
    simulated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class ScenarioState:
    scenario_id: str
    role: str
    label: str
    profile: Optional[ConstructionProfile] = None
    chainage: float = 0.0
    ring_no: int = 0
    seq_watermark: int = 0                  # 已处理读数最大 seq
    event_watermark_ms: int = 0             # 已处理读数最大事件时间
    strata: Optional[StrataColumn] = None
    prev_strata: Optional[StrataColumn] = None
    current_risk: Optional[RingRisk] = None
    seen_telemetry: Dict[str, bool] = field(default_factory=dict)
    ring_telemetry: Dict[int, List[Telemetry]] = field(default_factory=dict)
    risks: Dict[int, RingRisk] = field(default_factory=dict)
    anomalies: List[AnomalyRecord] = field(default_factory=list)
    anomalies_by_ring: Dict[int, List[AnomalyRecord]] = field(default_factory=dict)
    total_wear_mm: float = 0.0
    pending: List[Command] = field(default_factory=list)
    command_log: List[Command] = field(default_factory=list)
    stopped: bool = False
    pressure_hold: bool = False
    stable_nodes: List[StableNode] = field(default_factory=list)
    clean_run: int = 0
    forked_from: Optional[str] = None
    forks: List[str] = field(default_factory=list)
    advisory: List[str] = field(default_factory=list)
    quarantined: List[Dict[str, Any]] = field(default_factory=list)
    last_event_seq: int = 0


class TunnelService:
    def __init__(self, store: EventStore, line_id: str = "main-line",
                 clock_ms: Optional[Callable[[], int]] = None):
        self.store = store
        self.session_id = store.session_id
        self.line_id = line_id
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.scenarios: Dict[str, ScenarioState] = {}
        self._strata_seq: List[StrataColumn] = []
        self._cmd_counter = 0

    # ================================================================
    # 地层
    # ================================================================
    def load_strata_column(self, chainage: float) -> Optional[StrataColumn]:
        if not self._strata_seq:
            rows = self.store.load_strata(self.line_id)
            self._strata_seq = [StrataColumn.from_dict(r) for r in rows]
        match: Optional[StrataColumn] = None
        for col in self._strata_seq:
            if col.chainage <= chainage:
                match = col
            else:
                break
        return match

    def import_strata(self, columns: List[StrataColumn]) -> None:
        cols = sorted(columns, key=lambda c: c.chainage)
        self._strata_seq = cols
        self.store.save_strata(self.line_id, [c.to_dict() for c in cols])

    # ================================================================
    # 生命周期：开线 / 重启
    # ================================================================
    def open_session(self, initial_profile: ConstructionProfile,
                     chainage: float = 0.0) -> ScenarioState:
        existing = [s for s in self.store.list_scenarios()]
        if existing:
            raise RuntimeError("会话已存在，请使用 restart() 接续")
        sid = "scn-main"
        self._open_scenario(sid, ScenarioRole.ACTIVE, "主控方案",
                            initial_profile, chainage, forked_from=None)
        return self.scenarios[sid]

    def _open_scenario(self, scenario_id: str, role: ScenarioRole, label: str,
                       profile: ConstructionProfile, chainage: float,
                       forked_from: Optional[str], base_seq: int = 0) -> ScenarioState:
        st = ScenarioState(scenario_id=scenario_id, role=role.value, label=label,
                           profile=profile, chainage=chainage, forked_from=forked_from,
                           last_event_seq=base_seq)
        st.strata = self.load_strata_column(chainage)
        self.scenarios[scenario_id] = st
        self.store.append(
            scenario_id, 0, self.clock_ms(), "scenario_opened",
            {"role": role.value, "label": label, "chainage": chainage,
             "profile": profile.to_dict(), "forked_from": forked_from})
        if forked_from:
            self.scenarios[forked_from].forks.append(scenario_id)
        return st

    @classmethod
    def restart(cls, root_dir: str, session_id: str,
                clock_ms: Optional[Callable[[], int]] = None) -> "TunnelService":
        """服务重启：重放事件日志，完整还原所有方案状态与未决指令。"""
        store = EventStore(root_dir, session_id)
        svc = cls(store, clock_ms=clock_ms)
        rows = store.load_strata("main-line")
        svc._strata_seq = [StrataColumn.from_dict(r) for r in rows]
        for scenario_id in store.list_scenarios():
            svc._replay_scenario(scenario_id, store.load(scenario_id))
        max_n = 0
        for st in svc.scenarios.values():
            for cmd in st.command_log + st.pending:
                suffix = cmd.cmd_id.rsplit("-", 1)[-1]
                if suffix.isdigit():
                    max_n = max(max_n, int(suffix))
        svc._cmd_counter = max_n
        return svc

    def _replay_scenario(self, scenario_id: str, events: List[Event]) -> None:
        st: Optional[ScenarioState] = None
        for ev in events:
            st = self._apply_event(ev, st)
        if st is not None:
            st.last_event_seq = events[-1].seq if events else 0
            self.scenarios[scenario_id] = st

    # ================================================================
    # 事件应用（实时与重启重放共用同一路径）
    # ================================================================
    def _append(self, st: ScenarioState, ring_no: int, event_time_ms: int,
                type_: str, payload: Dict[str, Any],
                source_uid: Optional[str] = None) -> Event:
        ev = self.store.append(st.scenario_id, ring_no, event_time_ms,
                               type_, payload, source_uid)
        st.last_event_seq = ev.seq
        return ev

    def _apply_event(self, ev: Event, st: Optional[ScenarioState]) -> ScenarioState:
        p = ev.payload
        if ev.type == "scenario_opened":
            st = ScenarioState(
                scenario_id=ev.scenario_id, role=p["role"], label=p["label"],
                chainage=p["chainage"], profile=ConstructionProfile.from_dict(p["profile"]),
                forked_from=p.get("forked_from"))
            st.strata = self.load_strata_column(p["chainage"])
            if st.forked_from and st.forked_from in self.scenarios:
                self.scenarios[st.forked_from].forks.append(st.scenario_id)
            st.last_event_seq = ev.seq
            return st

        if ev.type == "telemetry_ingested":
            t = Telemetry.from_dict(p["telemetry"])
            st.seen_telemetry[ev.source_uid or t.source_uid] = True
            st.ring_telemetry.setdefault(t.ring_no, []).append(t)
            st.ring_no = max(st.ring_no, t.ring_no)
            st.chainage = t.chainage
            st.seq_watermark = max(st.seq_watermark, t.seq)
            if t.event_time_ms <= st.event_watermark_ms + LATE_TOLERANCE_MS \
                    or not st.ring_telemetry.get(t.ring_no):
                st.event_watermark_ms = max(st.event_watermark_ms, t.event_time_ms)
            if p.get("status") == ReadStatus.QUARANTINED.value:
                st.quarantined.append({"source_uid": ev.source_uid, "reason": p.get("reason")})
            return st

        if ev.type == "strata_entered":
            st.prev_strata = st.strata
            st.strata = StrataColumn.from_dict(p["strata"]) if p.get("strata") else st.strata
            return st

        if ev.type == "risk_recomputed":
            risk = RingRisk(**{k: v for k, v in p["risk"].items()})
            st.current_risk = risk
            st.risks[ev.ring_no] = risk
            st.total_wear_mm = round(
                sum(r.wear_per_ring_mm for r in st.risks.values()), 3)
            return st

        if ev.type == "anomaly":
            rec = AnomalyRecord(
                anomaly_uid=p["anomaly_uid"], ring_no=ev.ring_no, seq=p["seq"],
                event_time_ms=p["event_time_ms"], kinds=p["kinds"],
                breach_factors=p["breach_factors"],
                factor_chain=p["factor_chain"],
                before_snapshot=p["before_snapshot"], after_snapshot=p["after_snapshot"],
                retroactive=p.get("retroactive", False), resolved=p.get("resolved", False))
            st.anomalies.append(rec)
            st.anomalies_by_ring.setdefault(ev.ring_no, []).append(rec)
            return st

        if ev.type == "anomaly_revised":
            for rec in st.anomalies:
                if rec.anomaly_uid == p["anomaly_uid"]:
                    rec.after_snapshot = p["after_snapshot"]
                    rec.factor_chain = p["factor_chain"]
                    rec.breach_factors = p["breach_factors"]
                    rec.kinds = p["kinds"]
                    rec.retroactive = True
            return st

        if ev.type == "commands_resolved":
            for cd in p["commands"]:
                cmd = Command(cmd_id=cd["cmd_id"], action=cd["action"],
                              origin=cd["origin"], value=cd.get("value"),
                              issued_ms=cd.get("issued_ms", 0), note=cd.get("note", ""),
                              ack=cd.get("ack"), reason=cd.get("reason", ""),
                              simulated=cd.get("simulated", False))
                st.command_log.append(cmd)
            st.pending = [c for c in st.pending
                          if c.cmd_id not in {x["cmd_id"] for x in p["commands"]}]
            return st

        if ev.type == "command_submitted":
            cd = p["command"]
            cmd = Command(cmd_id=cd["cmd_id"], action=cd["action"],
                          origin=cd["origin"], value=cd.get("value"),
                          issued_ms=cd.get("issued_ms", 0), note=cd.get("note", ""))
            known = {c.cmd_id for c in st.pending} | \
                {c.cmd_id for c in st.command_log}
            if cmd.cmd_id not in known:
                st.pending.append(cmd)
            return st

        if ev.type == "command_issued":
            self._apply_command_effect(st, p["action"], p.get("value"))
            return st

        if ev.type == "ring_closed":
            st.clean_run = p.get("clean_run", st.clean_run)
            if p.get("stable_node"):
                node = StableNode(**p["stable_node"])
                st.stable_nodes = [n for n in st.stable_nodes if n.label != node.label]
                st.stable_nodes.append(node)
            return st

        if ev.type == "scenario_promoted":
            old = p.get("demoted")
            if old and old in self.scenarios and old != st.scenario_id:
                self.scenarios[old].role = ScenarioRole.WHATIF.value
                st.role = ScenarioRole.ACTIVE.value
            elif old == st.scenario_id:
                st.role = ScenarioRole.WHATIF.value
            return st

        if ev.type == "note":
            st.advisory.append(p["text"])
            return st

        return st

    # ================================================================
    # 指令执行效果（accepted 之后）
    # ================================================================
    def _apply_command_effect(self, st: ScenarioState, action: str,
                              value: Optional[float]) -> None:
        if action == "emergency_stop":
            st.stopped = True
        elif action == "resume":
            st.stopped = False
        elif action == "pressure_hold":
            st.pressure_hold = True
        elif action == "reset_lockdown":
            st.stopped = False
            st.pressure_hold = False
        elif value is not None and st.profile is not None:
            prof = st.profile
            if action == "set_pressure":
                prof.chamber_pressure_bar = self._clamp(value, prof.pressure_bounds)
            elif action == "set_speed":
                prof.advance_speed_mm_min = self._clamp(value, prof.speed_bounds)
            elif action == "set_torque":
                prof.torque_kNm = self._clamp(value, prof.torque_bounds)
            elif action == "set_grout":
                prof.grout_m3_per_ring = self._clamp(value, prof.grout_bounds)

    @staticmethod
    def _clamp(value: float, bounds: Tuple[float, float]) -> float:
        return round(min(max(value, bounds[0]), bounds[1]), 3)

    # ================================================================
    # 快照与连锁原因链
    # ================================================================
    def _snapshot(self, st: ScenarioState, t: Telemetry,
                  risk: Optional[RingRisk]) -> Dict[str, Any]:
        return StateSnapshot(
            ring_no=t.ring_no, seq=t.seq, event_time_ms=t.event_time_ms,
            chainage=t.chainage,
            profile=st.profile.to_dict() if st.profile else {},
            telemetry=t.to_dict(),
            risk=risk.to_dict() if risk else {},
            strata=st.strata.to_dict() if st.strata else {},
            protections_active={"stopped": st.stopped, "pressure_hold": st.pressure_hold},
            flags={"clean_run": st.clean_run},
        ).to_dict()

    def _build_factor_chain(self, nodes: List[FactorNode]) -> List[Dict[str, Any]]:
        """把并列的越界因素整理为有因果顺序的连锁链：地层突变 -> 参数越界 -> 风险后果。"""
        order = {"strata_change": 0, "pressure_vs_target": 1}
        consequence = {"settlement": 9, "water_inrush": 9}
        def rank(n: FactorNode) -> int:
            if n.factor in order:
                return order[n.factor]
            return consequence.get(n.factor, 5)
        chain: List[Dict[str, Any]] = []
        prev_factor = "地层/工况"
        for node in sorted(nodes, key=rank):
            chain.append({
                "factor": node.factor, "observed": node.observed,
                "threshold": node.threshold, "direction": node.direction,
                "cause": prev_factor, "effect": node.note,
            })
            prev_factor = node.factor
        return chain

    # ================================================================
    # 传感数据进入（含迟到/重复/错序处理）
    # ================================================================
    def dispatch_telemetry(self, t: Telemetry) -> Dict[str, str]:
        """一路传感读数扇出到全部方案（每个方案独立判定迟到/去重）。"""
        result: Dict[str, str] = {}
        active = [s for s in self.scenarios.values() if s.role == ScenarioRole.ACTIVE.value]
        others = [s for s in self.scenarios.values() if s.role != ScenarioRole.ACTIVE.value]
        for st in active + others:
            if st.stopped:
                # 停机中的读数仍入库用于事后分析，但不触发推进
                result[st.scenario_id] = self._ingest_ignored(st, t, "machine_stopped")
                continue
            self._catch_up_to(st, t.ring_no)
            result[st.scenario_id] = self.ingest_telemetry(st.scenario_id, t)
        return result

    def _ingest_ignored(self, st: ScenarioState, t: Telemetry, reason: str) -> str:
        self._append(st, t.ring_no, t.event_time_ms, "telemetry_ingested",
                     {"telemetry": t.to_dict(), "status": ReadStatus.QUARANTINED.value,
                      "reason": reason}, source_uid=t.source_uid)
        st.seen_telemetry[t.source_uid] = True
        st.quarantined.append({"source_uid": t.source_uid, "reason": reason})
        return ReadStatus.QUARANTINED.value

    def _catch_up_to(self, st: ScenarioState, target_ring: int) -> None:
        """whatif 方案落后时，用主控方案已记录的读数补齐到目标环。"""
        active = next((s for s in self.scenarios.values()
                       if s.role == ScenarioRole.ACTIVE.value), None)
        if active is None or st is active:
            return
        while st.ring_no < target_ring:
            next_ring = st.ring_no + 1
            samples = sorted(active.ring_telemetry.get(next_ring, []),
                             key=lambda x: x.seq)
            if not samples:
                return
            for sample in samples:
                if sample.source_uid not in st.seen_telemetry:
                    self.ingest_telemetry(st.scenario_id, sample, replayed=True)

    def ingest_telemetry(self, scenario_id: str, t: Telemetry,
                         replayed: bool = False) -> str:
        st = self.scenarios[scenario_id]
        # 1) 重复
        if t.source_uid in st.seen_telemetry:
            self._append(st, t.ring_no, t.event_time_ms, "telemetry_ingested",
                         {"telemetry": t.to_dict(),
                          "status": ReadStatus.QUARANTINED.value,
                          "reason": "duplicate"}, source_uid=t.source_uid)
            st.seen_telemetry[t.source_uid] = True
            st.quarantined.append({"source_uid": t.source_uid, "reason": "duplicate"})
            return ReadStatus.QUARANTINED.value

        # 2) 跳环（缺环）——隔离等待补齐
        expected_ring = st.ring_no + (1 if st.ring_telemetry else 0) if st.ring_no else t.ring_no
        if st.ring_no and t.ring_no > st.ring_no + 1:
            self._append(st, t.ring_no, t.event_time_ms, "telemetry_ingested",
                         {"telemetry": t.to_dict(),
                          "status": ReadStatus.QUARANTINED.value,
                          "reason": f"ring_gap expected<={st.ring_no + 1}"},
                         source_uid=t.source_uid)
            st.seen_telemetry[t.source_uid] = True
            st.quarantined.append({"source_uid": t.source_uid, "reason": "ring_gap"})
            return ReadStatus.QUARANTINED.value

        # 3) 迟到判定（事件时间早于当前水位线）
        late = t.event_time_ms < st.event_watermark_ms
        too_old = late and (st.event_watermark_ms - t.event_time_ms) > LATE_TOLERANCE_MS
        retroactive = late and not too_old
        if too_old:
            self._append(st, t.ring_no, t.event_time_ms, "telemetry_ingested",
                         {"telemetry": t.to_dict(),
                          "status": ReadStatus.QUARANTINED.value,
                          "reason": "beyond_late_tolerance"},
                         source_uid=t.source_uid)
            st.seen_telemetry[t.source_uid] = True
            st.quarantined.append(
                {"source_uid": t.source_uid, "reason": "beyond_late_tolerance"})
            return ReadStatus.QUARANTINED.value

        self._append(st, t.ring_no, t.event_time_ms, "telemetry_ingested",
                     {"telemetry": t.to_dict(),
                      "status": ReadStatus.LATE.value if late else ReadStatus.ON_TIME.value,
                      "replayed": replayed,
                      "retroactive": retroactive},
                     source_uid=t.source_uid)
        st.seen_telemetry[t.source_uid] = True
        st.ring_telemetry.setdefault(t.ring_no, []).append(t)
        st.ring_no = max(st.ring_no, t.ring_no)
        st.chainage = t.chainage
        st.seq_watermark = max(st.seq_watermark, t.seq)
        if not late:
            st.event_watermark_ms = max(st.event_watermark_ms, t.event_time_ms)

        self._evaluate_tick(st, t, retroactive=retroactive)
        return ReadStatus.LATE.value if late else ReadStatus.ON_TIME.value

    # ================================================================
    # 单步推演：地层 -> 风险 -> 越界/异常 -> 自动保护
    # ================================================================
    def _evaluate_tick(self, st: ScenarioState, t: Telemetry,
                       retroactive: bool) -> None:
        strata = self.load_strata_column(t.chainage) or st.strata
        if not retroactive and strata and (
                st.strata is None or strata.chainage != st.strata.chainage):
            st.prev_strata = st.strata
            st.strata = strata
            self._append(st, t.ring_no, t.event_time_ms, "strata_entered",
                         {"chainage": strata.chainage, "strata": strata.to_dict()},
                         source_uid=t.source_uid)

        assert st.profile is not None and st.strata is not None
        # 迟到数据归属较早桩号时，地层突变对比应按该读数位置的前序地层，
        # 而不是当前环已进入的新地层
        tick_strata = self.load_strata_column(t.chainage) or st.strata
        tick_prev = self.load_strata_column(t.chainage - 1.0) if retroactive \
            else st.prev_strata
        risk = predict_risk(t, st.profile, tick_strata, tick_prev)
        st.current_risk = risk
        st.risks[t.ring_no] = risk
        st.total_wear_mm = round(
            sum(r.wear_per_ring_mm for r in st.risks.values()), 3)
        self._append(st, t.ring_no, t.event_time_ms, "risk_recomputed",
                     {"risk": risk.to_dict()}, source_uid=t.source_uid)

        nodes = evaluate_breaches(t, st.profile, tick_strata, risk, tick_prev)
        if is_anomaly(nodes):
            self._raise_anomaly(st, t, risk, nodes, retroactive)
        elif retroactive:
            self._retract_if_cleared(st, t, risk, nodes)

    def _retract_if_cleared(self, st: ScenarioState, t: Telemetry,
                            risk: RingRisk, nodes: List[FactorNode]) -> None:
        """迟到数据修订后若该环不再构成异常，标记为已消解（保留原快照）。"""
        for rec in st.anomalies_by_ring.get(t.ring_no, []):
            if not rec.resolved:
                rec.resolved = True
                self._append(st, t.ring_no, t.event_time_ms, "anomaly_revised",
                             {"anomaly_uid": rec.anomaly_uid, "seq": t.seq,
                              "kinds": rec.kinds + ["cleared_by_late_data"],
                              "breach_factors": [n.factor for n in nodes],
                              "factor_chain": self._build_factor_chain(nodes),
                              "after_snapshot": self._snapshot(st, t, risk)},
                             source_uid=t.source_uid)

    def _raise_anomaly(self, st: ScenarioState, t: Telemetry, risk: RingRisk,
                       nodes: List[FactorNode], retroactive: bool) -> None:
        before = self._snapshot(st, t, st.risks.get(
            t.ring_no - 1, st.current_risk))
        if any(n.factor == "strata_change" for n in nodes) and st.prev_strata:
            before["strata"] = st.prev_strata.to_dict()
        factors = [n.factor for n in nodes]
        kinds: List[str] = []
        if any(n.factor == "strata_change" for n in nodes):
            kinds.append("strata_change")
        hard = [n for n in nodes if n.factor not in
                ("strata_change", "pressure_vs_target", "settlement", "water_inrush")]
        if len(hard) >= 2:
            kinds.append("joint_breach")
        critical = is_critical(nodes, risk)
        if critical:
            kinds.append("critical")

        uid = f"an-{st.scenario_id}-{len(st.anomalies) + 1}"
        rec = AnomalyRecord(
            anomaly_uid=uid, ring_no=t.ring_no, seq=t.seq,
            event_time_ms=t.event_time_ms, kinds=kinds, breach_factors=factors,
            factor_chain=self._build_factor_chain(nodes),
            before_snapshot=before, after_snapshot=self._snapshot(st, t, risk),
            retroactive=retroactive)
        st.anomalies.append(rec)
        st.anomalies_by_ring.setdefault(t.ring_no, []).append(rec)
        self._append(st, t.ring_no, t.event_time_ms, "anomaly",
                     {"anomaly_uid": uid, "seq": t.seq,
                      "event_time_ms": t.event_time_ms, "kinds": kinds,
                      "breach_factors": factors,
                      "factor_chain": self._build_factor_chain(nodes),
                      "before_snapshot": before,
                      "after_snapshot": self._snapshot(st, t, risk),
                      "retroactive": retroactive},
                     source_uid=t.source_uid)

        # 自动保护（仅真实主控方案下发现场；假设方案只出处置建议）
        if critical:
            if st.role == ScenarioRole.ACTIVE.value:
                self._auto_protection(st, t, risk)
            else:
                st.advisory.append(
                    f"[whatif:{st.scenario_id}] 环{t.ring_no} 触发严重异常，"
                    f"若为主控将执行紧急停机+保压；连锁：{' -> '.join(factors)}")
                self._append(st, t.ring_no, t.event_time_ms, "note",
                             {"text": st.advisory[-1]}, source_uid=t.source_uid)

    def _auto_protection(self, st: ScenarioState, t: Telemetry,
                         risk: RingRisk) -> None:
        now = t.event_time_ms
        stop = Command(self._new_cmd_id(), "emergency_stop", Origin.PROTECTION.value,
                       issued_ms=now,
                       note="严重异常自动保护：紧急停机")
        hold = Command(self._new_cmd_id(), "pressure_hold", Origin.PROTECTION.value,
                       issued_ms=now,
                       note="保压锁定，防止开挖面失稳/喷涌")
        # 保护指令直接进入待仲裁队列，与窗口内人工指令统一仲裁
        st.pending.extend([stop, hold])
        resolved = self._arbitrate(st, t.ring_no, now)
        self._record_resolved(st, t.ring_no, now, resolved)
        for cmd in resolved:
            if cmd.ack == Ack.ACCEPTED.value:
                self._execute(st, cmd, t.ring_no, now)

    def _new_cmd_id(self) -> str:
        self._cmd_counter += 1
        return f"cmd-{self.session_id}-{self._cmd_counter}"

    # ================================================================
    # 人工/自动指令提交与仲裁
    # ================================================================
    SAFE_DIRECTION = {
        # 因素 -> 更安全的参数方向
        "chamber_pressure_low": ("set_pressure", 1.0),
        "chamber_pressure_high": ("set_pressure", -1.0),
        "pressure_vs_target": ("set_pressure", 1.0),
        "water_inrush": ("set_pressure", 1.0),
        "advance_speed": ("set_speed", -1.0),
        "settlement": ("set_speed", -1.0),
        "grout": ("set_grout", 1.0),
        "torque": ("set_torque", -1.0),
    }

    def submit_command(self, scenario_id: str, action: str,
                       origin: str = Origin.MANUAL.value,
                       value: Optional[float] = None,
                       note: str = "", issued_ms: Optional[int] = None) -> Command:
        """提交指令。指令先进入未决队列，在关环（或自动保护）时统一仲裁。

        重启后未决指令从日志重建，仍会参与下一窗口仲裁。
        """
        st = self.scenarios[scenario_id]
        cmd = Command(self._new_cmd_id(), action, origin, value,
                      issued_ms or self.clock_ms(), note)
        st.pending.append(cmd)
        self._append(st, st.ring_no, cmd.issued_ms, "command_submitted",
                     {"command": cmd.to_dict()})
        return cmd

    def close_ring(self, scenario_id: str,
                   now_ms: Optional[int] = None) -> Dict[str, Any]:
        """关环：仲裁窗口内全部指令，记录稳定节点，推进剖面区段。"""
        st = self.scenarios[scenario_id]
        now = now_ms or self.clock_ms()
        resolved = self._arbitrate(st, st.ring_no, now)
        self._record_resolved(st, st.ring_no, now, resolved)
        for cmd in resolved:
            if cmd.ack == Ack.ACCEPTED.value:
                self._execute(st, cmd, st.ring_no, now)

        had_anomaly = bool(st.anomalies_by_ring.get(st.ring_no))
        st.clean_run = 0 if had_anomaly else st.clean_run + 1
        stable_node = None
        if not had_anomaly and st.clean_run >= STABLE_RUN_RINGS:
            node = StableNode(
                label=f"node-r{st.ring_no}", ring_no=st.ring_no,
                seq=st.seq_watermark, chainage=st.chainage,
                profile=st.profile.to_dict(),
                risk=st.current_risk.to_dict() if st.current_risk else {},
                strata_code=st.strata.code if st.strata else "",
                consecutive_clean_rings=st.clean_run, recorded_ms=now)
            st.stable_nodes = [n for n in st.stable_nodes if n.label != node.label]
            st.stable_nodes.append(node)
            stable_node = node.to_dict() if hasattr(node, "to_dict") else node.__dict__.copy()

        self._append(st, st.ring_no, now, "ring_closed",
                     {"clean_run": st.clean_run, "stable_node": stable_node,
                      "total_wear_mm": st.total_wear_mm})
        return {"ring": st.ring_no, "clean_run": st.clean_run,
                "resolved": [c.to_dict() for c in resolved],
                "stable_node": stable_node is not None}

    def _arbitrate(self, st: ScenarioState, ring_no: int, now: int) -> List[Command]:
        """同窗仲裁：紧急停机最高优先；保护 > 人工；同因素按安全方向裁决。

        假设方案（whatif）中的指令一律标记 simulated，不下发现场。
        """
        pending = list(st.pending)
        # 严重程度排序：emergency > 其余；保护源优先；先提交优先
        severity = {"emergency_stop": 0, "resume": 3, "reset_lockdown": 3}
        pending.sort(key=lambda c: (
            severity.get(c.action, 2),
            0 if c.origin == Origin.PROTECTION.value else 1,
            c.issued_ms, c.cmd_id))

        stop = next((c for c in pending if c.action == "emergency_stop"), None)
        accepted: List[Command] = []
        for cmd in pending:
            if stop is not None and cmd is not stop and cmd.action in (
                    "set_pressure", "set_speed", "set_torque", "set_grout"):
                cmd.ack = Ack.SKIPPED_AFTER_STOP.value
                cmd.reason = "同窗存在紧急停机，非安全指令跳过"
                continue

            if cmd.origin == Origin.MANUAL.value and st.stopped \
                    and cmd.action not in ("resume", "reset_lockdown"):
                cmd.ack = Ack.REJECTED_BY_EMERGENCY.value
                cmd.reason = "保护停机锁定中：仅允许复位/复工指令"
                continue

            # 保护与人工冲突：按当前异常的安全方向裁决
            blocker = self._find_protection_blocker(st, cmd)
            if blocker is not None:
                if cmd.origin == Origin.PROTECTION.value:
                    pass  # 保护指令不被保护阻断
                else:
                    cmd.ack = Ack.REJECTED_BY_PROTECTION.value
                    cmd.reason = f"与自动保护 {blocker.cmd_id}({blocker.action}) 冲突：{blocker.note}"
                    continue

            # 同因素人工/自动调参冲突：安全方向优先，方向相同则保护优先
            conflict = self._find_same_factor_conflict(accepted, cmd)
            if conflict is not None:
                cmd.ack = Ack.SUPERSEDED_BY_SAFER.value
                cmd.reason = f"同因素冲突，采用更安全的 {conflict.cmd_id}({conflict.origin})"
                continue

            if st.role != ScenarioRole.ACTIVE.value:
                cmd.simulated = True
            cmd.ack = Ack.ACCEPTED.value
            accepted.append(cmd)
        return pending

    def _find_protection_blocker(self, st: ScenarioState,
                                 cmd: Command) -> Optional[Command]:
        """人工指令若试图削弱现行保护，则被阻断。"""
        if cmd.origin != Origin.MANUAL.value:
            return None
        active_protections = [c for c in st.command_log
                              if c.origin == Origin.PROTECTION.value
                              and c.ack == Ack.ACCEPTED.value]
        for prot in active_protections:
            if prot.action == "emergency_stop" and cmd.action in (
                    "set_speed", "set_torque", "resume"):
                return prot
            if prot.action == "pressure_hold" and cmd.action == "set_pressure":
                return prot
        # 与同窗口保护指令冲突（如人工降速保压时反向加速）
        for other in st.pending:
            if other is cmd or other.origin != Origin.PROTECTION.value:
                continue
            if other.action == "pressure_hold" and cmd.action == "set_pressure":
                return other
        return None

    def _find_same_factor_conflict(self, accepted: List[Command],
                                   cmd: Command) -> Optional[Command]:
        same = [a for a in accepted if a.action == cmd.action
                and a.origin != cmd.origin]
        if not same:
            return None
        # 保护优先；同源则先到先得
        if cmd.origin != Origin.PROTECTION.value:
            return same[0]
        return None

    def _record_resolved(self, st: ScenarioState, ring_no: int, now: int,
                         resolved: List[Command]) -> None:
        st.pending = [c for c in st.pending
                      if c.cmd_id not in {x.cmd_id for x in resolved}]
        st.command_log.extend(resolved)
        self._append(st, ring_no, now, "commands_resolved",
                     {"commands": [c.to_dict() for c in resolved]})

    def _execute(self, st: ScenarioState, cmd: Command,
                 ring_no: int, now: int) -> None:
        self._apply_command_effect(st, cmd.action, cmd.value)
        self._append(st, ring_no, now, "command_issued",
                     {"cmd_id": cmd.cmd_id, "action": cmd.action,
                      "origin": cmd.origin, "value": cmd.value,
                      "simulated": cmd.simulated,
                      "profile_after": st.profile.to_dict() if st.profile else None})

    # ================================================================
    # 预测区段 / 下一阶段参数调整
    # ================================================================
    def preview_segment(self, profile: ConstructionProfile,
                        chainage: float, sample: Optional[Telemetry] = None
                        ) -> Optional[RingRisk]:
        """沿线路选择预测区段：用候选剖面在指定桩号试算风险，不改动运行状态。"""
        strata = self.load_strata_column(chainage)
        if strata is None:
            return None
        if sample is None:
            sample = Telemetry(
                source_uid="preview", seq=-1, ring_no=-1,
                event_time_ms=self.clock_ms(), chainage=chainage,
                torque_kNm=profile.torque_kNm,
                advance_speed_mm_min=profile.advance_speed_mm_min,
                chamber_pressure_bar=profile.chamber_pressure_bar,
                grout_m3=profile.grout_m3_per_ring)
        prev = self.load_strata_column(chainage - 1.0)
        return predict_risk(sample, profile, strata, prev)

    def adjust_next_stage(self, scenario_id: str,
                          profile: ConstructionProfile) -> ConstructionProfile:
        """调整下一阶段掘进参数（更新该方案的施工剖面）。"""
        st = self.scenarios[scenario_id]
        st.profile = profile
        self._append(st, st.ring_no, self.clock_ms(), "profile_adjusted",
                     {"profile": profile.to_dict()})
        return profile

    # ================================================================
    # 从历史稳定节点分叉新方案（多方案同步推进）
    # ================================================================
    def fork_from_stable_node(self, parent_id: str, node_label: str,
                              new_id: str, label: str,
                              profile: Optional[ConstructionProfile] = None,
                              role: ScenarioRole = ScenarioRole.WHATIF
                              ) -> ScenarioState:
        parent = self.scenarios[parent_id]
        node = next((n for n in parent.stable_nodes if n.label == node_label), None)
        if node is None:
            raise KeyError(f"稳定节点 {node_label} 不存在（仅可从稳定节点分叉）")

        events = self.store.load(parent_id)
        # 只复制稳定节点形成之前（含该环）的事件，之后的异常/保护不带入新方案
        allowed_at_node = {
            "telemetry_ingested", "strata_entered", "risk_recomputed",
            "commands_resolved", "command_issued", "command_submitted",
            "ring_closed"}
        copied = [
            e for e in events
            if e.type not in ("scenario_opened", "scenario_promoted")
            and (e.ring_no < node.ring_no
                 or (e.ring_no == node.ring_no and e.type in allowed_at_node))]
        # 按新方案流重新编号；事件内容保留原貌（uid/连锁关系可追溯）
        fresh = ConstructionProfile.from_dict(node.profile) if profile is None else profile
        st = self._open_scenario(new_id, role, label, fresh, node.chainage,
                                 forked_from=parent_id, base_seq=1)
        # 回放复制的事件，重建到稳定节点时的状态
        for idx, ev in enumerate(copied, start=2):
            rewritten = Event(
                uid=f"e-{new_id}-{idx}", session_id=self.session_id,
                scenario_id=new_id, seq=idx, ring_no=ev.ring_no,
                event_time_ms=ev.event_time_ms, type=ev.type,
                payload=ev.payload, source_uid=ev.source_uid)
            self.store._atomic_append(self.store._path(new_id), rewritten.to_line())
            self._apply_event(rewritten, st)
            st.last_event_seq = idx
        # 分叉后的参数差异
        if profile is not None:
            self._append(st, st.ring_no, self.clock_ms(), "profile_adjusted",
                         {"profile": profile.to_dict(), "reason": "fork_profile"})
        self._append(st, st.ring_no, self.clock_ms(), "note",
                     {"text": f"自 {parent_id}@{node_label} 分叉为方案「{label}」"})
        # 新方案从干净状态起步，不继承主控后续的保护锁定
        st.stopped = False
        st.pressure_hold = False
        return st

    def promote_scenario(self, scenario_id: str) -> None:
        """将假设方案提升为主控（原主控降级为假设），切换真实盾构机绑定。"""
        target = self.scenarios[scenario_id]
        previous = next((s for s in self.scenarios.values()
                         if s.role == ScenarioRole.ACTIVE.value
                         and s.scenario_id != scenario_id), None)
        now = self.clock_ms()
        target.role = ScenarioRole.ACTIVE.value
        self._append(target, target.ring_no, now, "scenario_promoted",
                     {"demoted": previous.scenario_id if previous else None})
        if previous is not None:
            previous.role = ScenarioRole.WHATIF.value
            self._append(previous, previous.ring_no, now, "scenario_promoted",
                         {"demoted": previous.scenario_id})

    # ================================================================
    # 状态视图
    # ================================================================
    def status(self, scenario_id: str) -> Dict[str, Any]:
        st = self.scenarios[scenario_id]
        return {
            "scenario_id": scenario_id,
            "role": st.role,
            "label": st.label,
            "ring_no": st.ring_no,
            "chainage": st.chainage,
            "profile": st.profile.to_dict() if st.profile else None,
            "strata": st.strata.to_dict() if st.strata else None,
            "risk": st.current_risk.to_dict() if st.current_risk else None,
            "total_wear_mm": st.total_wear_mm,
            "stopped": st.stopped,
            "pressure_hold": st.pressure_hold,
            "pending_commands": [c.to_dict() for c in st.pending],
            "clean_run": st.clean_run,
            "stable_nodes": [n.__dict__.copy() for n in st.stable_nodes],
            "anomalies": [a.to_dict() for a in st.anomalies],
            "quarantined": st.quarantined,
            "forks": st.forks,
            "forked_from": st.forked_from,
            "advisory": st.advisory,
        }
