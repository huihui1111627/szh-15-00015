"""风险模型、越界判定、自动保护规则与连锁原因分析。"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .models import Band, Breach, METRIC_CN, Reading, Stratum

RING_LENGTH_M = 1.5          # 每环掘进长度
REQUIRED_SAFE_READINGS = 3   # 保护解除所需连续安全读数数


def evaluate_reading(reading: Reading, band: Band) -> List[Breach]:
    """把一条读数与当前施工参数带比较，得到越界项。"""
    metrics = band.check(
        reading.torque,
        reading.advance_speed,
        reading.chamber_pressure,
        reading.grout_volume,
    )
    ranges = {
        "torque": band.torque,
        "advance_speed": band.advance_speed,
        "chamber_pressure": band.chamber_pressure,
        "grout_volume": band.grout_volume,
    }
    values = {
        "torque": reading.torque,
        "advance_speed": reading.advance_speed,
        "chamber_pressure": reading.chamber_pressure,
        "grout_volume": reading.grout_volume,
    }
    return [Breach(metric=m, value=values[m], band=ranges[m]) for m in metrics]


def ring_risks(
    n: int,
    torque_sum: float,
    torque_max: float,
    speed_sum: float,
    speed_min: float,
    pressure_sum: float,
    grout_sum: float,
    band: Band,
    stratum: Optional[Stratum],
    water_head_m: float,
) -> Tuple[float, float, str]:
    """环闭环时结算地表沉降、刀具磨损增量、涌水风险等级。

    返回 (本环沉降 mm, 本环磨损增量 mm, 涌水等级)。
    """
    avg_torque = torque_sum / n
    avg_speed = speed_sum / n
    avg_pressure = pressure_sum / n
    avg_grout = grout_sum / n

    # 刀具磨损：与地层磨蚀性、平均扭矩正相关，与推进速度负相关。
    abras = stratum.abrasiveness if stratum else 1.0
    wear = abras * RING_LENGTH_M * (avg_torque / 3500.0) * (35.0 / max(avg_speed, 1.0))

    # 地表沉降：土仓欠压为主因，叠加快推进与注浆不足。
    p_target = sum(band.chamber_pressure) / 2.0
    g_target = sum(band.grout_volume) / 2.0
    under_pressure = max(0.0, (p_target - avg_pressure) / p_target)
    grout_deficit = max(0.0, (g_target - avg_grout) / g_target)
    settlement = (
        100.0 * under_pressure
        + 0.06 * max(0.0, avg_speed - 40.0)
        + 1.8 * grout_deficit
    )

    # 涌水：渗透系数 × 水头，欠压时风险上调一级。
    k = stratum.permeability if stratum else 1e-6
    index = k * max(water_head_m, 0.0)
    if index < 1e-5:
        level = "低"
    elif index < 1e-3:
        level = "中"
    elif index < 1e-2:
        level = "高"
    else:
        level = "极高"
    if avg_pressure < band.chamber_pressure[0]:
        level = _bump_water_level(level)
    return round(settlement, 3), round(wear, 3), level


def _bump_water_level(level: str) -> str:
    return {"低": "中", "中": "高", "高": "极高", "极高": "极高"}[level]


def protections_for(
    breaches: List[Breach],
    reading: Reading,
    stratum_changed: bool,
    new_stratum: Optional[Stratum],
) -> List[Tuple[str, str]]:
    """根据越界与地层突变判定应触发的自动保护。"""
    actions: List[Tuple[str, str]] = []
    by_metric = {b.metric: b for b in breaches}

    if "torque" in by_metric and reading.torque > new_stratum_band(new_stratum):
        actions.append(("cutter_overload_trip", f"刀盘扭矩{reading.torque:g}超限，停转并降推力"))

    if len(breaches) >= 2:
        names = "、".join(METRIC_CN[b.metric] for b in breaches)
        actions.append(("joint_limit_trip", f"多项参数共同越界（{names}），联锁降速保压"))

    permeable = new_stratum is not None and new_stratum.permeability >= 1e-4
    if permeable and "chamber_pressure" in by_metric and reading.chamber_pressure < by_metric["chamber_pressure"].band[0]:
        actions.append(("water_inrush_guard", f"透水地层中土仓欠压至{reading.chamber_pressure:g}bar，升压防涌水"))

    if stratum_changed and new_stratum is not None and new_stratum.permeability >= 1e-4:
        actions.append(("stratum_change_guard", f"突入{new_stratum.name}，冻结自动推进等待参数带复核"))
    return actions


def new_stratum_band(stratum: Optional[Stratum]) -> float:
    return stratum.torque[1] if stratum is not None else float("inf")


def build_joint_chain(breaches: List[Breach], reading: Reading, stratum_name: str) -> List[Tuple[str, str]]:
    """构造多参数共同越界的连锁原因链。"""
    chain: List[Tuple[str, str]] = [("多项参数越界", "、".join(b.describe() for b in breaches))]
    over = [b for b in breaches if b.metric in ("torque", "advance_speed")]
    under = [b for b in breaches if b.metric == "chamber_pressure"]
    grout = [b for b in breaches if b.metric == "grout_volume"]
    if over:
        chain.append(("切削荷载异常", f"在{stratum_name}中高扭矩/快推进，姿态与荷载耦合恶化"))
    if under:
        chain.append(("土仓保压不足", "开挖面支护压力下降"))
        chain.append(("地表沉降风险上升", "欠压引起地层损失，沉降槽扩大"))
    if grout:
        chain.append(("背填注浆不足", "盾尾空隙填充不密实，固结沉降叠加"))
    if any(b.metric == "torque" for b in breaches):
        chain.append(("刀具磨损加速", "超载切削使滚刀偏磨与轴承温升加剧"))
    chain.append(("自动保护联锁", "joint_limit_trip 降速保压，人工指令在保护解除前被抑制"))
    return chain


def build_stratum_change_chain(
    old: Optional[Stratum], new: Stratum, breaches: List[Breach]
) -> List[Tuple[str, str]]:
    """构造地层突变的连锁原因链。"""
    old_name = old.name if old else "未知"
    chain: List[Tuple[str, str]] = [
        ("地层突变", f"开挖面由{old_name}变为{new.name}，预测剖面与实际不符")
    ]
    chain.append(("施工参数带失效", f"沿用{old_name}参数带，与{new.name}的可掘性不匹配"))
    if breaches:
        chain.append(("参数连锁越界", "、".join(b.describe() for b in breaches)))
    if new.permeability >= 1e-4:
        chain.append(("涌水通道形成", f"{new.name}渗透系数{new.permeability:g}m/s，富水条件下风险升高"))
    if new.abrasiveness >= 1.4:
        chain.append(("刀具冲击磨损", "卵砾石非均质切削，滚刀冲击载荷增大"))
    chain.append(("剖面修正", "以实测地层重建参数带，区段预测同步更新"))
    return chain


def snapshot_state(ctx: Dict[str, object]) -> Dict[str, object]:
    """截取关键字段作为异常前/后状态（只保留可序列化的标量与列表）。"""
    keep = (
        "t", "ring", "chainage", "stratum", "torque", "advance_speed",
        "chamber_pressure", "grout_volume", "targets", "active_protections",
        "settlement_total", "wear_total", "water_risk",
    )
    snap: Dict[str, object] = {}
    for key in keep:
        if key in ctx:
            snap[key] = ctx[key]
    return snap
