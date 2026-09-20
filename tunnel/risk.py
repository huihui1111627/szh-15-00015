"""风险推演：沉降 / 刀具磨损 / 涌水，以及越界判定与连锁原因链。

公式为物理启发式的确定性近似，输入输出均为不可变数据，方便单元测试，
也可在不改动 service.py 的前提下替换为真实预测模型。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .models import (
    ConstructionProfile, StrataColumn, Telemetry, RingRisk, FactorNode,
)

# 每环掘进长度（m）
RING_LENGTH_M = 1.5


# ---------------------------------------------------------------- 地层

def strata_changed(prev: Optional[StrataColumn], cur: StrataColumn) -> bool:
    """地层突变：编码变化，或关键参数跃变超过阈值。"""
    if prev is None:
        return False
    if prev.code != cur.code:
        return True
    if prev.cohesion > 0:
        if abs(cur.cohesion - prev.cohesion) / max(prev.cohesion, 1e-9) > 0.35:
            return True
    if cur.permeability > 5 * max(prev.permeability, 1e-9):
        return True
    if abs(cur.abrasivity - prev.abrasivity) > 0.25:
        return True
    return False


def target_pressure_bar(strata: StrataColumn) -> float:
    """目标土仓压力 ≈ (覆土自重应力 + 水压力) 的工程近似（bar）。"""
    gamma_soil = 18.0   # kN/m3
    gamma_w = 9.81
    p_kpa = gamma_soil * strata.cover_depth + gamma_w * strata.water_head
    return max(0.2, p_kpa / 100.0 * 0.8)


# ---------------------------------------------------------------- 单环风险

def predict_risk(t: Telemetry, profile: ConstructionProfile,
                 strata: StrataColumn, prev: Optional[StrataColumn]) -> RingRisk:
    """由传感读数 + 当前剖面 + 地层推演单环风险。"""
    p0 = target_pressure_bar(strata)
    p_ratio = t.chamber_pressure_bar / max(p0, 1e-9)
    pressure_deficit = max(0.0, 1.0 - p_ratio)
    pressure_excess = max(0.0, p_ratio - 1.0)

    # 注浆填充率（相对剖面设定）
    grout_ratio = t.grout_m3 / max(profile.grout_m3_per_ring, 1e-9)
    grout_deficit = max(0.0, 1.0 - grout_ratio)

    # 超速比例
    speed_ratio = t.advance_speed_mm_min / max(profile.advance_speed_mm_min, 1e-9)
    speed_excess = max(0.0, speed_ratio - 1.0)

    # 软弱程度：黏聚力越低、模量越低越敏感
    weakness = max(0.0, 1.0 - strata.cohesion / 80.0)

    # --- 地表沉降（Peck 槽谷中心）-------------------------------------
    # 体积损失率：欠压、欠注浆、超速在软弱地层中叠加
    vl = (0.004
          + 0.05 * pressure_deficit
          + 0.04 * grout_deficit
          + 0.02 * speed_excess) * (0.6 + 0.8 * weakness)
    vl = min(vl, 0.08)
    width_k = 0.5
    z0 = max(strata.cover_depth, 1.0)
    i = width_k * z0
    excavation_area = math.pi * 3.1 ** 2  # 近似开挖面积 m2（6.2m 级盾构）
    vs = vl * excavation_area * RING_LENGTH_M
    settlement = vs / (math.sqrt(2 * math.pi) * i) * 1000.0  # mm

    # --- 刀具磨损（本环增量）-------------------------------------------
    torque_ratio = t.torque_kNm / max(profile.torque_kNm, 1e-9)
    wear = (0.02
            + 0.18 * strata.abrasivity
            + 0.01 * max(torque_ratio, 1.0) ** 2) * RING_LENGTH_M

    # --- 涌水风险 ------------------------------------------------------
    driving_head = max(0.0, strata.water_head * 9.81 - t.chamber_pressure_bar * 100.0)
    driving = min(1.0, driving_head / (strata.water_head * 9.81 + 1e-9))
    inrush = min(1.0, strata.permeability / 5.0 * 0.6 + driving * 0.5)

    changed = strata_changed(prev, strata)
    stability = 1.0 - min(
        1.0,
        0.45 * pressure_deficit + 0.2 * pressure_excess
        + 0.25 * grout_deficit + 0.2 * speed_excess
        + (0.15 if changed else 0.0),
    )

    return RingRisk(
        ring_no=t.ring_no,
        settlement_mm=round(settlement, 3),
        wear_per_ring_mm=round(wear, 3),
        water_inrush_risk=round(inrush, 3),
        stability_index=round(stability, 3),
        detail={
            "target_pressure_bar": round(p0, 3),
            "pressure_ratio": round(p_ratio, 3),
            "grout_ratio": round(grout_ratio, 3),
            "speed_ratio": round(speed_ratio, 3),
            "volume_loss": round(vl, 4),
            "weakness": round(weakness, 3),
            "strata_changed": 1.0 if changed else 0.0,
        },
    )


# ---------------------------------------------------------------- 越界判定

# 各因素：显示名、观测值、阈值、方向、说明
def evaluate_breaches(t: Telemetry, profile: ConstructionProfile,
                      strata: StrataColumn, risk: RingRisk,
                      prev: Optional[StrataColumn]) -> List[FactorNode]:
    nodes: List[FactorNode] = []

    if strata_changed(prev, strata):
        nodes.append(FactorNode(
            "strata_change", 0.0, 0.0, "changed",
            f"地层由 {prev.code if prev else '-'} 突变为 {strata.code}："
            f"c={strata.cohesion}kPa, k={strata.permeability}m/d, "
            f"磨蚀={strata.abrasivity}",
        ))

    checks = [
        ("torque", t.torque_kNm, profile.torque_bounds[1],
         "high", "刀盘扭矩超过剖面硬上界，硬岩/结泥饼风险"),
        ("advance_speed", t.advance_speed_mm_min, profile.speed_bounds[1],
         "high", "推进速度超过剖面硬上界，出渣/保压失衡"),
        ("chamber_pressure_high", t.chamber_pressure_bar,
         profile.pressure_bounds[1], "high",
         "土仓压力超过硬上界，超挖隆起/喷涌风险"),
        ("chamber_pressure_low", t.chamber_pressure_bar,
         profile.pressure_bounds[0], "low",
         "土仓压力低于硬下界，开挖面失稳沉降风险"),
        ("grout", t.grout_m3, profile.grout_bounds[0],
         "low", "注浆量低于硬下界，盾尾空隙填充不足"),
    ]
    for factor, observed, limit, direction, note in checks:
        if direction == "high" and observed > limit:
            nodes.append(FactorNode(factor, round(observed, 3), limit, "high", note))
        if direction == "low" and observed < limit:
            nodes.append(FactorNode(factor, round(observed, 3), limit, "low", note))

    # 软边界（偏离目标），计入连锁链但单独不构成异常
    p0 = risk.detail.get("target_pressure_bar", 0.0)
    if p0 and t.chamber_pressure_bar < 0.85 * p0:
        nodes.append(FactorNode(
            "pressure_vs_target", round(t.chamber_pressure_bar, 3),
            round(0.85 * p0, 3), "low",
            "实际土仓压力低于目标值的 85%，开挖面支护不足"))
    if risk.settlement_mm > 30.0:
        nodes.append(FactorNode(
            "settlement", risk.settlement_mm, 30.0, "high",
            "预测地表沉降超过 30mm 控制值"))
    if risk.water_inrush_risk > 0.7:
        nodes.append(FactorNode(
            "water_inrush", risk.water_inrush_risk, 0.7, "high",
            "涌水风险超过 0.7，需关注高渗透地层与保压"))
    return nodes


# 触发异常所需的非地层因素共同越界数量
JOINT_BREACH_MIN = 2


def is_anomaly(nodes: List[FactorNode]) -> bool:
    """地层突变即异常；否则需 >=2 项参数共同越界。"""
    if any(n.factor == "strata_change" for n in nodes):
        return True
    hard = [n for n in nodes if n.factor != "strata_change"
            and n.factor not in ("pressure_vs_target", "settlement", "water_inrush")]
    return len(hard) >= JOINT_BREACH_MIN


def is_critical(nodes: List[FactorNode], risk: RingRisk) -> bool:
    """严重异常：触发自动保护（紧急停机/保压）。"""
    factors = {n.factor for n in nodes}
    if "strata_change" in factors and len(nodes) >= 2:
        return True
    if risk.water_inrush_risk >= 0.85 and "chamber_pressure_low" in factors:
        return True
    if risk.settlement_mm >= 50.0:
        return True
    if len([n for n in nodes if n.factor not in
            ("pressure_vs_target", "settlement", "water_inrush")]) >= 3:
        return True
    return False
