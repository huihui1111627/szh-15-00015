"""事件溯源引擎：重放施工事件得到各方案的确定性状态。

所有外部输入（读数、人工指令、保护确认、预测区段、克隆）都先落事件日志，
再由 fold 纯函数重放。服务重启后只需重新加载日志即可接续全部状态。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .models import (
    DEFAULT_STRATA,
    Band,
    Breach,
    Incident,
    Protection,
    Reading,
    RingStat,
    Segment,
    Stratum,
)
from .rules import (
    REQUIRED_SAFE_READINGS,
    build_joint_chain,
    build_stratum_change_chain,
    evaluate_reading,
    protections_for,
    ring_risks,
    snapshot_state,
)
from .store import EventStore

# 事件排序：同一工程时刻下，先收读数，再处理指令，最后读取派生审计事件。
EVENT_RANK = {
    "reading": 0,
    "adjust_command": 1,
    "protection_ack": 1,
    "protection_triggered": 2,
    "joint_incident": 2,
    "stratum_incident": 2,
    "ring_closed": 2,
    "manual_decision": 2,
    "stable_node": 2,
    "late_annotation": 2,
    "forecast_set": 3,
    "scenario_created": 4,
}


@dataclass
class ScenarioState:
    """单个施工方案的完整可重放状态。"""

    name: str
    strata: Dict[str, Stratum] = field(default_factory=lambda: dict(DEFAULT_STRATA))
    forecast: List[Segment] = field(default_factory=list)
    water_heads: Dict[str, float] = field(default_factory=dict)
    overrides: Dict[str, Band] = field(default_factory=dict)
    rings: Dict[int, RingStat] = field(default_factory=dict)
    incidents: Dict[str, Incident] = field(default_factory=dict)
    protections: Dict[str, Protection] = field(default_factory=dict)
    commands: List[dict] = field(default_factory=list)
    stable_nodes: List[dict] = field(default_factory=list)
    created_from: Optional[str] = None
    created_at_ring: Optional[int] = None

    last_t: int = -1
    watermark: int = -1
    current_ring: Optional[int] = None
    current_stratum: Optional[str] = None
    last_reading_ctx: Dict[str, object] = field(default_factory=dict)
    targets: Dict[str, float] = field(default_factory=dict)
    active_audit: List[str] = field(default_factory=list)
    ring_chainage_end: Dict[int, float] = field(default_factory=dict)

    settlement_total: float = 0.0
    wear_total: float = 0.0
    water_risk_latest: str = ""

    def band_for(self, stratum_code: Optional[str]) -> Band:
        """当前生效参数带：实测/预测地层默认带，叠加人工调整。"""
        if stratum_code is None:
            stratum_code = self.current_stratum
        if stratum_code is None:
            stratum_code = "clay"
        if stratum_code in self.overrides:
            return self.overrides[stratum_code]
        stratum = self.strata.get(stratum_code)
        if stratum is None:
            stratum = DEFAULT_STRATA["clay"]
        return Band(
            torque=stratum.torque,
            advance_speed=stratum.advance_speed,
            chamber_pressure=stratum.chamber_pressure,
            grout_volume=stratum.grout_volume,
        )

    def segment_at(self, chainage: float) -> Optional[Segment]:
        for segment in self.forecast:
            if segment.covers(chainage):
                return segment
        return None

    def predicted_stratum(self, chainage: float) -> Optional[str]:
        segment = self.segment_at(chainage)
        return segment.stratum_code if segment else None

    def water_head_for(self, chainage: float) -> float:
        segment = self.segment_at(chainage)
        if segment and segment.seg_id in self.water_heads:
            return self.water_heads[segment.seg_id]
        return 15.0

    def active_protection_names(self) -> List[str]:
        return [p.name for p in self.protections.values() if p.active(REQUIRED_SAFE_READINGS)]

    def closed_rings_sorted(self) -> List[RingStat]:
        return sorted((r for r in self.rings.values() if r.closed), key=lambda r: r.ring)


def _event_sort_key(event: dict) -> Tuple[int, int]:
    return (event["ts"], EVENT_RANK.get(event["type"], 5))


class Engine:
    """施工剖面控制引擎。"""

    LATE_HORIZON_MIN = 30

    def __init__(self, store: EventStore):
        self.store = store
        self.states: Dict[str, ScenarioState] = {}
        self._audit_baseline: Dict[str, dict] = {}
        for name in store.scenarios():
            self._audit_baseline[name] = self._signatures(self.fold(name))
            self.states[name] = self.fold(name)

    # ------------------------------------------------------------------
    # 对外命令
    # ------------------------------------------------------------------

    def create_scenario(self, name: str, forecast: List[Segment],
                        water_heads: Optional[Dict[str, float]] = None) -> ScenarioState:
        if name in self.states:
            raise ValueError(f"方案 {name} 已存在")
        payload = {
            "forecast": [seg.__dict__ for seg in forecast],
            "water_heads": water_heads or {},
        }
        self.store.append(name, 0, "forecast_set", payload)
        self.store.append(name, 0, "scenario_created", {})
        self._recompute(name)
        return self.states[name]

    def submit_reading(self, scenario: str, reading: Reading) -> Tuple[ScenarioState, Optional[str]]:
        """接收传感读数；超出迟到窗口的读数只入档不参与计算。"""
        state = self.states[scenario]
        stratum_code = state.predicted_stratum(
            reading.chainage) or state.current_stratum or "clay"
        if reading.t < state.watermark - self.LATE_HORIZON_MIN:
            t_d = state.watermark - reading.t
            note = (
                f"迟到{t_d}分钟超过窗口{self.LATE_HORIZON_MIN}分钟，"
                "仅入档不参与已闭环风险结算"
            )
            payload = reading.__dict__
            payload["excluded_late"] = True
            self.store.append(scenario, reading.t, "late_annotation",
                              {"reading": payload, "note": note},
                              source=reading.source)
            self._recompute(scenario)
            return self.states[scenario], note

        duplicate = any(
            e["type"] == "reading" and e["payload"].get("t") == reading.t
            for e in self.store.by_scenario(scenario)
        )
        if duplicate:
            return state, f"工程时刻{reading.t}读数已存在，忽略重复报文"

        note = None
        if reading.t < state.watermark:
            note = (
                f"迟到{state.watermark - reading.t}分钟（窗口内），"
                "按事件时间重放并修订相关异常/环评估"
            )
            self.store.append(scenario, reading.t, "late_annotation",
                              {"reading": reading.__dict__, "note": note},
                              source=reading.source)

        self.store.append(scenario, reading.t, "reading", reading.__dict__,
                          source=reading.source)
        self._recompute(scenario)
        return self.states[scenario], note

    def adjust(self, scenario: str, t: int, ring: int, author: str,
               torque: Tuple[float, float], advance_speed: Tuple[float, float],
               chamber_pressure: Tuple[float, float], grout_volume: Tuple[float, float],
               reason: str = "", force: bool = False,
               stratum: Optional[str] = None) -> Tuple[ScenarioState, dict]:
        """人工调整下一阶段参数带；保护生效期间默认被抑制。"""
        state = self.states[scenario]
        stratum_code = stratum or state.current_stratum or "clay"
        active = state.active_protection_names()
        band = Band(torque, advance_speed, chamber_pressure, grout_volume)
        payload = {
            "author": author,
            "ring": ring,
            "stratum": stratum_code,
            "band": band.as_dict(),
            "reason": reason,
            "active_protections": active,
            "force": force,
        }
        if active and not force:
            payload["outcome"] = "suppressed"
            payload["decision"] = (
                f"自动保护{ '、'.join(active) }生效，人工指令被抑制；"
                "保护解除并确认后可加 force 强制覆盖"
            )
        else:
            if active and force:
                payload["outcome"] = "forced_override"
                payload["decision"] = "操作员强制覆盖自动保护，按人工参数带推进"
                self.store.append(scenario, t, "protection_ack",
                                  {"author": author, "name": None, "force": True},
                                  source=author)
            else:
                payload["outcome"] = "accepted"
                payload["decision"] = "人工参数带已生效，自动保护继续监测"
        self.store.append(scenario, t, "adjust_command", payload, source=author)
        self._recompute(scenario)
        return self.states[scenario], payload

    def acknowledge_protection(self, scenario: str, t: int, author: str,
                               name: Optional[str] = None) -> ScenarioState:
        self.store.append(scenario, t, "protection_ack",
                          {"author": author, "name": name, "force": False}, source=author)
        self._recompute(scenario)
        return self.states[scenario]

    def clone_stable(self, source: str, new_name: str, t: int,
                     up_to_ring: int, note: str = "") -> ScenarioState:
        """从历史稳定节点复制出独立方案，之后各方案同步推进互不影响。"""
        if new_name in self.states:
            raise ValueError(f"方案 {new_name} 已存在")
        src_state = self.states[source]
        if not any(node["ring"] == up_to_ring for node in src_state.stable_nodes):
            raise ValueError(f"源方案在第{up_to_ring}环没有稳定节点，不允许复制")

        cloned: List[dict] = []
        for event in sorted(self.store.by_scenario(source), key=_event_sort_key):
            copy = deepcopy(event)
            payload = copy.get("payload", {})
            # 只保留稳定节点之前的原始事件；派生审计事件由重放重新生成。
            if event["type"] in (
                "protection_triggered", "joint_incident", "stratum_incident",
                "ring_closed", "stable_node", "late_annotation",
            ):
                continue
            if event["type"] == "protection_ack":
                continue
            if event["type"] == "adjust_command" and payload.get("ring", 0) > up_to_ring:
                continue
            if event["type"] == "reading" and (
                payload.get("ring", up_to_ring) > up_to_ring
                or payload.get("t", t) > t
            ):
                continue
            if event["ts"] > t:
                continue
            copy["scenario"] = new_name
            copy["id"] = f"{event['id']}_{new_name}"
            cloned.append(copy)

        self.store.rewrite_for_clone(cloned)
        self.store.append(new_name, t, "scenario_created",
                          {"cloned_from": source, "stable_ring": up_to_ring, "note": note})
        for name in self.store.scenarios():
            self._recompute(name)
        return self.states[new_name]

    # ------------------------------------------------------------------
    # 重放（纯函数语义）：状态完全由事件序列决定
    # ------------------------------------------------------------------

    def fold(self, scenario: str) -> ScenarioState:
        events = sorted(self.store.by_scenario(scenario), key=_event_sort_key)
        state = ScenarioState(name=scenario)
        ctx: Dict[str, object] = {}
        late_times = {
            e["payload"]["reading"]["t"]
            for e in events
            if e["type"] == "late_annotation"
            and not e["payload"].get("reading", {}).get("excluded_late")
        }
        for event in events:
            self._apply(state, event, ctx, late_times)
        return state

    def _recompute(self, scenario: str) -> ScenarioState:
        """重放后对比签名，把新发生的派生事实镜像进事件日志。"""
        new_state = self.fold(scenario)
        new_sig = self._signatures(new_state)
        old_sig = self._audit_baseline.get(scenario, {"incidents": {}, "protections": {}, "rings": {}, "stable": set()})

        for incident_id, sig in new_sig["incidents"].items():
            if incident_id not in old_sig["incidents"]:
                kind = sig[0]
                etype = "stratum_incident" if kind == "stratum_change" else "joint_incident"
                incident = new_state.incidents[incident_id]
                self.store.append(
                    scenario, incident.t, etype,
                    {
                        "incident_id": incident.incident_id,
                        "ring": incident.ring,
                        "chainage": incident.chainage,
                        "kind": incident.kind,
                        "metrics": incident.metrics,
                        "before": incident.before,
                        "after": incident.after,
                        "chain": incident.chain,
                        "amended": incident.amended,
                        "amendment_note": incident.amendment_note,
                    },
                )
            elif old_sig["incidents"][incident_id] != sig:
                incident = new_state.incidents[incident_id]
                self.store.append(
                    scenario, incident.t, "joint_incident",
                    {
                        "incident_id": incident.incident_id,
                        "ring": incident.ring,
                        "chainage": incident.chainage,
                        "kind": incident.kind,
                        "metrics": incident.metrics,
                        "before": incident.before,
                        "after": incident.after,
                        "chain": incident.chain,
                        "amended": True,
                        "amendment_note": "窗口内迟到读数重放后修订",
                        "revises": incident.incident_id,
                    },
                )

        for sig in new_sig["protections"]:
            if sig not in old_sig["protections"]:
                name, t_sig, ring, reason = sig
                self.store.append(
                    scenario, t_sig, "protection_triggered",
                    {"name": name, "ring": ring, "reason": reason},
                )

        for ring, sig in new_sig["rings"].items():
            if ring not in old_sig["rings"]:
                stat = new_state.rings[ring]
                self.store.append(
                    scenario, stat.close_t, "ring_closed",
                    {
                        "ring": ring,
                        "stratum": stat.stratum,
                        "settlement": stat.settlement,
                        "wear": stat.wear,
                        "water_risk": stat.water_risk,
                        "n": stat.n,
                        "breach_ratio": round(stat.breach_count / max(stat.n, 1), 3),
                    },
                )
            elif old_sig["rings"][ring] != sig:
                stat = new_state.rings[ring]
                self.store.append(
                    scenario, stat.close_t, "ring_closed",
                    {
                        "ring": ring,
                        "stratum": stat.stratum,
                        "settlement": stat.settlement,
                        "wear": stat.wear,
                        "water_risk": stat.water_risk,
                        "n": stat.n,
                        "breach_ratio": round(stat.breach_count / max(stat.n, 1), 3),
                        "revised_by_late": True,
                    },
                )

        for node in new_state.stable_nodes:
            key = node["ring"]
            if key not in old_sig["stable"]:
                self.store.append(scenario, node["t"], "stable_node", node)

        final_state = self.fold(scenario)
        self.states[scenario] = final_state
        self._audit_baseline[scenario] = self._signatures(final_state)
        return final_state

    @staticmethod
    def _signatures(state: ScenarioState) -> dict:
        return {
            "incidents": {
                incident_id: (
                    incident.kind, tuple(incident.metrics),
                    tuple((n, w) for n, w in incident.chain),
                    tuple(sorted(incident.after.items())),
                )
                for incident_id, incident in state.incidents.items()
            },
            "protections": [
                (p.name, p.t, p.ring, p.reason)
                for p in state.protections.values()
            ],
            "rings": {
                ring: (
                    round(stat.settlement, 3), round(stat.wear, 3),
                    stat.water_risk, stat.n, stat.breach_count, stat.stratum,
                    stat.close_t,
                )
                for ring, stat in state.rings.items() if stat.closed
            },
            "stable": {node["ring"] for node in state.stable_nodes},
        }

    def _apply(self, state: ScenarioState, event: dict,
               ctx: Dict[str, object], late_times: set) -> None:
        etype = event["type"]
        payload = event["payload"]
        if etype == "forecast_set":
            state.forecast = [Segment(**seg) for seg in payload["forecast"]]
            state.water_heads = dict(payload.get("water_heads", {}))
        elif etype == "scenario_created":
            state.created_from = payload.get("cloned_from")
            state.created_at_ring = payload.get("stable_ring")
        elif etype == "reading":
            if payload.get("excluded_late"):
                pass
            else:
                clean = {k: v for k, v in payload.items() if k != "excluded_late"}
                self._project_reading(state, Reading(**clean), event, ctx,
                                      is_late=event["ts"] in late_times)
        elif etype == "adjust_command":
            state.commands.append({"t": event["ts"], **payload})
            if payload.get("outcome") in ("accepted", "forced_override"):
                band = payload["band"]
                state.overrides[payload["stratum"]] = Band(
                    tuple(band["torque"]), tuple(band["advance_speed"]),
                    tuple(band["chamber_pressure"]), tuple(band["grout_volume"]),
                )
        elif etype == "manual_decision":
            state.targets = {k: sum(v) / 2.0 for k, v in payload["band"].items()}
        elif etype == "protection_ack":
            self._apply_ack(state, payload)
        elif etype == "late_annotation":
            state.active_audit.append(payload["note"])
        state.last_t = max(state.last_t, event["ts"])

    # ------------------------------------------------------------------
    # 读数投影：越界 -> 异常 -> 自动保护 -> 环闭环风险
    # ------------------------------------------------------------------

    @staticmethod
    def _context_snapshot(state: ScenarioState, reading: Reading) -> Dict[str, object]:
        return snapshot_state({
            "t": reading.t,
            "ring": reading.ring,
            "chainage": reading.chainage,
            "stratum": state.current_stratum,
            "torque": reading.torque,
            "advance_speed": reading.advance_speed,
            "chamber_pressure": reading.chamber_pressure,
            "grout_volume": reading.grout_volume,
            "targets": state.targets,
            "active_protections": state.active_protection_names(),
            "settlement_total": state.settlement_total,
            "wear_total": state.wear_total,
            "water_risk": state.water_risk_latest,
        })

    def _project_reading(self, state: ScenarioState, reading: Reading,
                         event: dict, ctx: Dict[str, object],
                         is_late: bool) -> None:
        # 进入新环：先结算上一环的沉降、磨损、涌水风险与稳定判定。
        if state.current_ring is not None and reading.ring > state.current_ring:
            self._close_ring(state, state.current_ring, reading.t, ctx)

        stat = state.rings.get(reading.ring)
        if stat is None:
            stat = RingStat(ring=reading.ring, stratum=None)
            state.rings[reading.ring] = stat

        before = dict(state.last_reading_ctx) or self._context_snapshot(state, reading)

        # 地层认定：优先采用实测，其次采用预测区段。
        predicted = state.predicted_stratum(reading.chainage)
        observed = reading.observed_stratum
        effective = observed or predicted
        if effective:
            stat.stratum_votes[effective] = stat.stratum_votes.get(effective, 0) + 1
            stat.stratum = max(stat.stratum_votes, key=stat.stratum_votes.get)

        stratum_changed = False
        if observed and state.current_stratum and observed != state.current_stratum:
            stratum_changed = True
        if effective:
            state.current_stratum = effective

        band = state.band_for(effective)
        if stat.band_snapshot is None:
            stat.band_snapshot = band.as_dict()
        breaches = evaluate_reading(reading, band)
        stat.n += 1
        stat.torque_sum += reading.torque
        stat.speed_sum += reading.advance_speed
        stat.pressure_sum += reading.chamber_pressure
        stat.grout_sum += reading.grout_volume
        stat.pressure_sum_sq += reading.chamber_pressure ** 2
        stat.torque_max = max(stat.torque_max, reading.torque)
        stat.speed_min = min(stat.speed_min, reading.advance_speed)
        stat.breach_count += len(breaches)
        state.ring_chainage_end[reading.ring] = reading.chainage

        new_stratum = state.strata.get(effective) if effective else None
        active_now = set(state.active_protection_names())

        # 地层突变：即使没有越界也单独留痕。
        if stratum_changed:
            old_code = before.get("stratum")
            old_stratum = state.strata.get(old_code) if old_code else None
            chain = build_stratum_change_chain(
                old_stratum,
                new_stratum or DEFAULT_STRATA.get(effective or "", DEFAULT_STRATA["clay"]),
                breaches,
            )
            after = self._context_snapshot(state, reading)
            self._record_incident(
                state, reading, "stratum_change",
                [b.metric for b in breaches], before, after, chain,
                is_late=is_late, t=reading.t,
            )

        # 多项参数共同越界（两项及以上）：同一环归并为一条持续更新的异常留痕。
        if len(breaches) >= 2:
            stratum_name = new_stratum.name if new_stratum else "未知地层"
            chain = build_joint_chain(breaches, reading, stratum_name)
            after = self._context_snapshot(state, reading)
            self._record_incident(
                state, reading, "joint_breach",
                [b.metric for b in breaches], before, after, chain,
                is_late=is_late, t=reading.t, key_ring=True,
            )

        # 自动保护：触发后锁定，需连续安全读数 + 人工确认才解除。
        for name, reason in protections_for(breaches, reading, stratum_changed, new_stratum):
            if name not in active_now:
                state.protections[name] = Protection(
                    name=name, t=reading.t, ring=reading.ring, reason=reason
                )
                active_now.add(name)

        self._update_protection_counters(state, reading, breaches, active_now)

        # 开环期间的滚动涌水指示（闭环时正式结算）。
        if stat.n:
            _, _, water = ring_risks(
                max(stat.n, 1), stat.torque_sum, stat.torque_max,
                stat.speed_sum, stat.speed_min, stat.pressure_sum,
                stat.grout_sum, band, new_stratum,
                state.water_head_for(reading.chainage),
            )
            state.water_risk_latest = water

        state.last_reading_ctx = self._context_snapshot(state, reading)
        ctx.update(state.last_reading_ctx)
        state.current_ring = reading.ring
        state.watermark = max(state.watermark, event["ts"])

    @staticmethod
    def _record_incident(state: ScenarioState, reading: Reading, kind: str,
                         metrics: List[str], before: Dict[str, object],
                         after: Dict[str, object],
                         chain: List[Tuple[str, str]],
                         is_late: bool, t: int, key_ring: bool = False) -> None:
        if key_ring:
            incident_id = f"INC-R{reading.ring:03d}-{kind}"
        else:
            incident_id = f"INC-R{reading.ring:03d}-{kind}-{t}"
        note = "由窗口内迟到读数重放后补充/修订" if is_late else ""
        existing = state.incidents.get(incident_id)
        if existing is not None:
            existing.after, existing.chain = after, chain
            existing.metrics = metrics
            if is_late:
                existing.amended = True
                existing.amendment_note = note or "窗口内迟到读数重放后修订"
            return
        state.incidents[incident_id] = Incident(
            incident_id=incident_id, ring=reading.ring,
            chainage=reading.chainage, kind=kind, t=t, metrics=metrics,
            before=before, after=after, chain=chain,
            amended=is_late, amendment_note=note,
        )
        if is_late and not note:
            state.incidents[incident_id].amendment_note = "窗口内迟到读数重放后补充"

    def _update_protection_counters(self, state: ScenarioState, reading: Reading,
                                    breaches: List[Breach],
                                    active_now: set) -> None:
        band = state.band_for(state.current_stratum)
        safe = not evaluate_reading(reading, band)
        for name, protection in state.protections.items():
            if not protection.active(REQUIRED_SAFE_READINGS):
                continue
            if safe:
                protection.safe_readings += 1
            else:
                protection.safe_readings = 0

    def _close_ring(self, state: ScenarioState, ring: int, t: int,
                    ctx: Dict[str, object]) -> None:
        stat = state.rings[ring]
        stratum = state.strata.get(stat.stratum) if stat.stratum else None
        band = Band(**stat.band_snapshot) if stat.band_snapshot else state.band_for(stat.stratum)
        chainage_end = state.ring_chainage_end.get(ring, 0.0)
        settlement, wear, water = ring_risks(
            stat.n, stat.torque_sum, stat.torque_max, stat.speed_sum,
            stat.speed_min, stat.pressure_sum, stat.grout_sum, band,
            stratum, state.water_head_for(chainage_end),
        )
        stat.settlement, stat.wear, stat.water_risk = settlement, wear, water
        stat.closed = True
        stat.close_t = t
        state.settlement_total += settlement
        state.wear_total += wear
        state.water_risk_latest = water

        ring_incidents = [i for i in state.incidents.values() if i.ring == ring]
        ratio = (stat.breach_count / stat.n) if stat.n else 0.0
        is_stable = (
            not ring_incidents
            and not state.active_protection_names()
            and ratio < 0.2
        )
        stat.stable = is_stable
        if is_stable:
            state.stable_nodes.append({
                "ring": ring,
                "t": t,
                "chainage": chainage_end,
                "stratum": stat.stratum,
                "settlement": settlement,
                "wear": wear,
                "water_risk": water,
            })

    def _apply_ack(self, state: ScenarioState, payload: dict) -> None:
        name = payload.get("name")
        forced = payload.get("force", False)
        for protection in state.protections.values():
            if name is not None and protection.name != name:
                continue
            protection.acked = True
            if forced:
                protection.safe_readings = REQUIRED_SAFE_READINGS
