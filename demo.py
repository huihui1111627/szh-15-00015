#!/usr/bin/env python3
"""端到端演示：沿线路施工剖面、风险随推进更新、异常连锁、迟到数据、
人工/保护仲裁、稳定节点分叉多方案、重启接续。

运行：python3 demo.py
"""
import json
import os
import shutil

from tunnel import (
    TunnelService, EventStore, Origin, ScenarioRole,
    StrataColumn, ConstructionProfile, Telemetry,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data_demo")
SESSION = "line-demo"
T0 = 1_700_000_000_000
MINUTE = 60_000


def line(title):
    print("\n" + "=" * 12 + f" {title} " + "=" * 12)


def t(seq, ring, chainage, **kw):
    kw.setdefault("event_time_ms", T0 + ring * 10 * MINUTE + seq * 1000)
    kw.setdefault("torque_kNm", 3100)
    kw.setdefault("advance_speed_mm_min", 44)
    kw.setdefault("chamber_pressure_bar", 2.28)
    kw.setdefault("grout_m3", 5.3)
    kw.setdefault("thrust_kN", 28000)
    return Telemetry(source_uid=f"t-{seq}", seq=seq, ring_no=ring,
                     chainage=chainage, **kw)


def brief(svc, sid):
    s = svc.status(sid)
    risk = s["risk"] or {}
    print(f"  [{s['label']}] 环{s['ring_no']} 桩号{s['chainage']}m "
          f"地层={s['strata']['code'] if s['strata'] else '-'} "
          f"沉降={risk.get('settlement_mm')}mm 磨损累计={s['total_wear_mm']}mm "
          f"涌水风险={risk.get('water_inrush_risk')} 停机={s['stopped']}")


def main():
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)
    svc = TunnelService(EventStore(DATA_DIR, SESSION),
                        clock_ms=lambda: T0 + 100 * MINUTE)
    svc.import_strata([
        StrataColumn(0.0, "C-1", "粉质黏土", 42, 18, 0.05, 0.20, 14, 6),
        StrataColumn(30.0, "S-2", "富水中粗砂", 6, 28, 3.2, 0.55, 15, 9),
        StrataColumn(60.0, "R-3", "风化岩", 120, 120, 0.02, 0.85, 16, 4),
    ])
    profile = ConstructionProfile(
        "seg-clay", 0, 30, torque_kNm=3200, advance_speed_mm_min=45,
        chamber_pressure_bar=2.3, grout_m3_per_ring=5.2,
        torque_bounds=(0, 5200), speed_bounds=(5, 70),
        pressure_bounds=(1.2, 3.2), grout_bounds=(4.5, 7.0))
    svc.open_session(profile, chainage=0.0)

    line("选择预测区段：对富水砂层比较两套剖面")
    safe = ConstructionProfile("seg-sand", 30, 60, 3300, 30, 2.6, 6.0,
                               pressure_bounds=(1.2, 3.4))
    risky = ConstructionProfile("seg-sand", 30, 60, 3300, 65, 1.5, 4.8,
                                pressure_bounds=(1.2, 3.4))
    rs = svc.preview_segment(safe, 31.5)
    rr = svc.preview_segment(risky, 31.5)
    print(f"  保守方案：沉降={rs.settlement_mm}mm, 涌水={rs.water_inrush_risk}, "
          f"稳定指数={rs.stability_index}")
    print(f"  激进方案：沉降={rr.settlement_mm}mm, 涌水={rr.water_inrush_risk}, "
          f"稳定指数={rr.stability_index}")

    line("稳定推进 1~3 环（风险随推进逐环更新，形成稳定节点）")
    for r in range(1, 4):
        svc.dispatch_telemetry(t(r * 10, r, r * 1.5))
        svc.close_ring("scn-main", now_ms=T0 + r * 10 * MINUTE + MINUTE)
        brief(svc, "scn-main")
    node = svc.status("scn-main")["stable_nodes"][-1]["label"]
    print(f"  已形成稳定节点：{node}")

    line("从稳定节点复制出两套方案，同步推进")
    svc.fork_from_stable_node("scn-main", node, "scn-safe", "保压减速方案",
                              profile=safe)
    svc.fork_from_stable_node("scn-main", node, "scn-fast", "提速抢工方案",
                              profile=risky)

    line("第4环进入富水砂层；主控发生 地层突变+低压+欠注浆 连锁异常")
    bad = t(40, 4, 31.5, chamber_pressure_bar=1.05, grout_m3=4.4,
            advance_speed_mm_min=62)
    svc.dispatch_telemetry(bad)
    for sid in ("scn-main", "scn-safe", "scn-fast"):
        brief(svc, sid)
    anomaly = svc.status("scn-main")["anomalies"][-1]
    print("  异常类型：", "+".join(anomaly["kinds"]))
    print("  连锁原因链：")
    for link in anomaly["factor_chain"]:
        print(f"    {link['cause']} -> {link['factor']}: {link['effect']}")
    print("  异常前地层/压力：",
          anomaly["before_snapshot"]["strata"]["code"],
          anomaly["before_snapshot"]["telemetry"]["chamber_pressure_bar"])
    print("  异常后地层/压力：",
          anomaly["after_snapshot"]["strata"]["code"],
          anomaly["after_snapshot"]["telemetry"]["chamber_pressure_bar"])
    print("  自动保护：停机=", svc.status("scn-main")["stopped"],
          "保压=", svc.status("scn-main")["pressure_hold"])
    print("  whatif 严重异常仅出处置建议（不下发现场）：")
    for note in svc.status("scn-fast")["advisory"]:
        print("   ", note)

    line("同一仲裁窗口：人工提速 vs 自动保护 -> 人工被拒，记录原因")
    svc.submit_command("scn-main", "set_speed", Origin.MANUAL.value,
                       value=68, note="工长要求提速")
    svc.submit_command("scn-main", "reset_lockdown", Origin.MANUAL.value,
                       note="处置完成，复位保护")
    out = svc.close_ring("scn-main", now_ms=T0 + 40 * MINUTE + 2 * MINUTE)
    for c in out["resolved"]:
        print(f"  {c['origin']:>10} {c['action']:<15} -> {c['ack']}  {c['reason']}")

    line("迟到数据：环3 一条延迟上报的高扭矩读数（追溯修订）")
    late = t(31, 3, 4.5, torque_kNm=5800, advance_speed_mm_min=72,
             event_time_ms=T0 + 3 * 10 * MINUTE + 30_000)
    print("  处理状态：", svc.ingest_telemetry("scn-main", late))
    rec = svc.status("scn-main")["anomalies"][-1]
    print("  追溯异常 retroactive =", rec["retroactive"],
          "因素：", rec["breach_factors"])
    print("  重复上报隔离：", svc.ingest_telemetry("scn-main", late))

    line("服务重启：重放事件日志后状态完整接续")
    svc2 = TunnelService.restart(DATA_DIR, SESSION,
                                 clock_ms=lambda: T0 + 200 * MINUTE)
    print("  恢复方案：", sorted(svc2.scenarios))
    for sid in ("scn-main", "scn-safe", "scn-fast"):
        brief(svc2, sid)
    s = svc2.status("scn-main")
    print(f"  异常记录={len(s['anomalies'])} 稳定节点={len(s['stable_nodes'])} "
          f"总磨损={s['total_wear_mm']}mm 隔离读数={len(s['quarantined'])}")

    line("重启后继续推进，并将保守方案提升为主控")
    svc2.promote_scenario("scn-safe")
    t5 = t(50, 5, 33.0, chamber_pressure_bar=2.55, grout_m3=6.0,
           advance_speed_mm_min=30)
    svc2.dispatch_telemetry(t5)
    for sid in ("scn-safe", "scn-main"):
        svc2.close_ring(sid, now_ms=T0 + 50 * MINUTE)
        brief(svc2, sid)
    print("  当前主控：",
          [sid for sid, x in svc2.scenarios.items()
           if x.role == ScenarioRole.ACTIVE.value])


if __name__ == "__main__":
    main()
