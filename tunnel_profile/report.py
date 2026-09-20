"""把方案状态渲染成可操作的文字剖面。"""
from __future__ import annotations

from typing import List

from .engine import ScenarioState
from .models import METRIC_CN


def render_state(state: ScenarioState) -> str:
    lines: List[str] = []
    lines.append(f"方案 {state.name}")
    if state.created_from:
        lines.append(f"  复制自 {state.created_from} 的第{state.created_at_ring}环稳定节点")
    lines.append(f"  当前环 {state.current_ring}  当前地层 {state.current_stratum}")
    band = state.band_for(state.current_stratum)
    lines.append("  当前参数带:")
    for key in ("torque", "advance_speed", "chamber_pressure", "grout_volume"):
        lo, hi = getattr(band, key)
        lines.append(f"    {METRIC_CN[key]}: [{lo:g}, {hi:g}]")
    lines.append(
        f"  累计沉降 {state.settlement_total:.2f} mm | 累计磨损 {state.wear_total:.2f} mm"
        f" | 最新涌水风险 {state.water_risk_latest or '-'}"
    )
    active = state.active_protection_names()
    lines.append(f"  自动保护: {'、'.join(active) if active else '无'}")
    lines.append(f"  稳定节点: {[n['ring'] for n in state.stable_nodes]}")
    if state.rings:
        lines.append("  各环结果:")
        for ring in sorted(state.rings):
            stat = state.rings[ring]
            tag = "闭环" if stat.closed else "开环"
            lines.append(
                f"    R{ring:03d} {stat.stratum} {tag} "
                f"沉降={stat.settlement:g}mm 磨损={stat.wear:g}mm "
                f"涌水={stat.water_risk or '-'} 稳定={getattr(stat, 'stable', False)}"
            )
    incidents = sorted(state.incidents.values(), key=lambda i: (i.ring, i.t))
    if incidents:
        lines.append("  异常留痕:")
        for incident in incidents:
            flag = "（迟到数据修订）" if incident.amended else ""
            lines.append(f"    {incident.incident_id} {incident.kind}{flag}")
            for step in incident.chain_detail():
                lines.append(f"      {step}")
    return "\n".join(lines)
