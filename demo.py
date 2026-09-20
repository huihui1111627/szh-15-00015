#!/usr/bin/env python3
"""端到端演示：施工剖面 -> 沿区段调参 -> 风险更新 -> 异常留痕 ->
多方案并行 -> 迟到数据 -> 人工/自动仲裁 -> 重启续算。

用法：python3 demo.py [事件日志路径]
"""
from __future__ import annotations

import sys
import tempfile

from tunnel_profile import Engine, EventStore, Reading, Segment
from tunnel_profile.report import render_state
from tunnel_profile.simulator import FaultSpec, LineSimulator


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    log_path = sys.argv[1] if len(sys.argv) > 1 else tempfile.mktemp(
        prefix="tunnel_", suffix=".jsonl")
    print(f"事件日志: {log_path}")

    forecast = [
        Segment("S1", 0.0, 16.0, "clay"),
        Segment("S2", 16.0, 32.0, "silt"),
        Segment("S3", 32.0, 48.0, "sand"),
    ]
    sim = LineSimulator(forecast, readings_per_ring=4,
                        minutes_per_reading=5, chainage_per_reading=1.5)
    store = EventStore(log_path)
    eng = Engine(store)

    section("① 建立主方案：选择预测区段（黏土→粉土→富水砂层）")
    eng.create_scenario("主方案", forecast, {"S3": 22.0})
    normal = sim.generate(17)  # t=0..80，进入粉土段
    for reading in normal:
        eng.submit_reading("主方案", reading)
    print(render_state(eng.states["主方案"]))

    section("② 沿线路调整下一阶段参数（粉土段降速保压，人工参数带生效）")
    _, decision = eng.adjust(
        "主方案", t=24, ring=2, author="李工",
        torque=(2000, 3400), advance_speed=(28, 40),
        chamber_pressure=(2.2, 3.0), grout_volume=(6.2, 8.5),
        reason="粉土段试掘进数据偏软，降速保压",
    )
    print("调参结果:", decision["outcome"], "-", decision["decision"])

    section("③ 地层突变 + 多项共同越界：t=85 起突入富水砂砾层")
    fault = FaultSpec(start=85, end=100, stratum="gravel",
                      torque_add=1600, speed_add=-12,
                      pressure_add=-1.2, grout_add=-1.8)
    for reading in sim.generate(4, faults=[fault], step_offset=17):
        if reading.t >= 85:
            state, _ = eng.submit_reading("主方案", reading)
    print("触发保护:", state.active_protection_names())
    incidents = sorted(state.incidents.values(), key=lambda i: (i.ring, i.t))
    for incident in incidents:
        print(f"\n[{incident.incident_id}] 里程{incident.chainage:g}m")
        print("  异常前:", {k: incident.before[k] for k in
                           ("ring", "stratum", "chamber_pressure", "active_protections")})
        print("  异常后:", {k: incident.after[k] for k in
                           ("ring", "stratum", "chamber_pressure", "active_protections")})
        print("  连锁原因:")
        for step in incident.chain_detail():
            print("   ", step)

    section("④ 人工指令与自动保护同时发生：保护优先，指令抑制并审计")
    _, decision = eng.adjust(
        "主方案", t=87, ring=5, author="王班长",
        torque=(4000, 5500), advance_speed=(20, 30),
        chamber_pressure=(3.2, 4.0), grout_volume=(8.5, 10.5),
        reason="想强行推过富水带",
    )
    print("仲裁结果:", decision["outcome"])
    print(decision["decision"])

    section("⑤ 传感数据迟到：补传 t=78 读数（22 分钟，窗口内→重放修订）")
    late = Reading(t=78, ring=4, chainage=23.4, torque=4200, advance_speed=44,
                   chamber_pressure=1.85, grout_volume=5.6, source="plc-backfill")
    _, note = eng.submit_reading("主方案", late)
    print(note)
    revised = eng.states["主方案"].incidents.get("INC-R004-joint_breach")
    print("第4环修订异常:", "已生成" if revised else "无",
          "| amended =", revised.amended if revised else "-")

    section("⑥ 超时迟到：补传 t=5 读数（超 30 分钟窗口→只入档）")
    very_late = Reading(t=5, ring=1, chainage=1.5, torque=2600, advance_speed=44,
                        chamber_pressure=2.0, grout_volume=6.0, source="plc-backfill")
    _, note = eng.submit_reading("主方案", very_late)
    print(note)

    section("⑦ 保护解除：连续 3 条安全读数 + 人工确认后恢复人工控制")
    safe_readings = [
        Reading(105, 6, 30.0, 3000, 34, 2.6, 7.2, observed_stratum="silt"),
        Reading(110, 6, 30.6, 2950, 33, 2.65, 7.3, observed_stratum="silt"),
        Reading(115, 6, 31.2, 2900, 32, 2.6, 7.1, observed_stratum="silt"),
    ]
    for r in safe_readings[:2]:
        eng.submit_reading("主方案", r)
    eng.acknowledge_protection("主方案", t=110, author="王班长")
    eng.submit_reading("主方案", safe_readings[2])
    print("解除后剩余保护:", eng.states["主方案"].active_protection_names())
    print("确认后人工调参:")
    _, decision = eng.adjust(
        "主方案", t=116, ring=6, author="王班长",
        torque=(2600, 3800), advance_speed=(22, 36),
        chamber_pressure=(2.6, 3.4), grout_volume=(7.0, 9.0),
        reason="恢复推进，保守穿带",
    )
    print("  ->", decision["outcome"])

    section("⑧ 从第1环稳定节点克隆保守方案，与主方案并行推进")
    eng.clone_stable("主方案", "保守方案", t=120, up_to_ring=1,
                     note="慢推高压对比试验")
    _, decision = eng.adjust(
        "保守方案", t=120, ring=2, author="赵总工",
        torque=(1800, 2800), advance_speed=(30, 38),
        chamber_pressure=(2.0, 2.6), grout_volume=(5.5, 7.0),
        reason="沿用黏土慢推参数",
    )
    print("克隆方案调参:", decision["outcome"])
    for idx, shifted_t in enumerate(range(125, 145, 5)):
        r = Reading(shifted_t, 2, 6.0 + idx * 1.5,
                    torque=2500, advance_speed=34,
                    chamber_pressure=2.3, grout_volume=6.4)
        eng.submit_reading("保守方案", r)
    print(render_state(eng.states["保守方案"]))

    section("⑨ 服务重启：仅用 JSONL 事件日志重建全部方案状态")
    restarted = Engine(EventStore(log_path))
    for name, s in restarted.states.items():
        print(f"- {name}: 当前环{s.current_ring} | 异常{len(s.incidents)}起 | "
              f"稳定节点{[n['ring'] for n in s.stable_nodes]} | "
              f"沉降{s.settlement_total:.2f}mm | 磨损{s.wear_total:.2f}mm | "
              f"保护{len(s.active_protection_names())}项")
    print("\n重启后继续推进 1 条读数，状态无缝接续：")
    r = sim.generate(1)[0]
    r.t, r.chainage, r.ring = 145, 43.5, 8
    restarted.submit_reading("主方案", r)
    print("主方案当前环:", restarted.states["主方案"].current_ring)


if __name__ == "__main__":
    main()
