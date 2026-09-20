"""盾构施工剖面控制系统的领域模型。

时间约定：所有时间戳为单调的工程时钟（分钟，整数），不依赖墙钟，
因此系统在服务重启后仍可对迟到传感数据做确定性处理。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Stratum:
    """地层类型及其默认施工参数带。"""

    code: str
    name: str
    torque: Tuple[float, float]            # kN·m
    advance_speed: Tuple[float, float]    # mm/min
    chamber_pressure: Tuple[float, float] # bar
    grout_volume: Tuple[float, float]     # m3/环
    permeability: float                   # m/s，用于涌水风险
    abrasiveness: float                   # 刀具磨损相对系数，1.0 为基准


@dataclass(frozen=True)
class Segment:
    """线路上的预测区段（里程区间）及预测地层。"""

    seg_id: str
    chainage_start: float  # m
    chainage_end: float    # m
    stratum_code: str

    def covers(self, chainage: float) -> bool:
        return self.chainage_start <= chainage < self.chainage_end


@dataclass(frozen=True)
class Band:
    """单项掘进参数的可操作上下限。"""

    torque: Tuple[float, float]
    advance_speed: Tuple[float, float]
    chamber_pressure: Tuple[float, float]
    grout_volume: Tuple[float, float]

    def check(self, torque: float, speed: float, pressure: float, grout: float) -> List[str]:
        """返回越界参数名列表。"""
        breaches: List[str] = []
        if not self.torque[0] <= torque <= self.torque[1]:
            breaches.append("torque")
        if not self.advance_speed[0] <= speed <= self.advance_speed[1]:
            breaches.append("advance_speed")
        if not self.chamber_pressure[0] <= pressure <= self.chamber_pressure[1]:
            breaches.append("chamber_pressure")
        if not self.grout_volume[0] <= grout <= self.grout_volume[1]:
            breaches.append("grout_volume")
        return breaches

    def as_dict(self) -> Dict[str, List[float]]:
        return {
            "torque": list(self.torque),
            "advance_speed": list(self.advance_speed),
            "chamber_pressure": list(self.chamber_pressure),
            "grout_volume": list(self.grout_volume),
        }


# 四类典型地层的默认施工剖面参数带。
DEFAULT_STRATA: Dict[str, Stratum] = {
    "clay": Stratum("clay", "黏土", (1800, 3200), (35, 60), (1.6, 2.6), (5.0, 7.5), 1e-8, 0.7),
    "silt": Stratum("silt", "粉土", (2400, 3800), (25, 45), (2.0, 3.2), (6.0, 8.5), 1e-6, 1.0),
    "sand": Stratum("sand", "砂层", (3000, 4600), (15, 35), (2.6, 3.8), (7.0, 9.5), 1e-4, 1.4),
    "gravel": Stratum("gravel", "砂砾/卵石", (3800, 6000), (8, 25), (3.0, 4.4), (8.0, 11.0), 1e-3, 2.2),
}

# 监测指标中文名，用于报告。
METRIC_CN = {
    "torque": "刀盘扭矩",
    "advance_speed": "推进速度",
    "chamber_pressure": "土仓压力",
    "grout_volume": "注浆量",
}


@dataclass
class Reading:
    """单条传感读数（刀盘扭矩、推进速度、土仓压力、注浆量、地层变化）。"""

    t: int
    ring: int
    chainage: float
    torque: float
    advance_speed: float
    chamber_pressure: float
    grout_volume: float
    observed_stratum: Optional[str] = None
    source: str = "plc"


@dataclass
class Breach:
    """单次越界记录。"""

    metric: str
    value: float
    band: Tuple[float, float]

    def describe(self) -> str:
        lo, hi = self.band
        return f"{METRIC_CN.get(self.metric, self.metric)}={self.value:g} 超出允许带[{lo:g},{hi:g}]"


@dataclass
class Incident:
    """异常事件：保留异常前后状态并给出连锁原因。"""

    incident_id: str
    ring: int
    chainage: float
    kind: str                 # stratum_change | joint_breach
    t: int
    metrics: List[str]
    before: Dict[str, object]
    after: Dict[str, object]
    chain: List[Tuple[str, str]]
    amended: bool = False
    amendment_note: str = ""

    def chain_text(self) -> str:
        return " -> ".join(node for node, _ in self.chain)

    def chain_detail(self) -> List[str]:
        return [f"{node}：{why}" for node, why in self.chain]


@dataclass
class Protection:
    """自动保护动作的锁定状态。"""

    name: str
    t: int
    ring: int
    reason: str
    safe_readings: int = 0
    acked: bool = False

    def active(self, required_safe: int = 3) -> bool:
        return not (self.acked and self.safe_readings >= required_safe)


@dataclass
class RingStat:
    """单环聚合统计与风险结果（风险在闭环时结算）。"""

    ring: int
    stratum: Optional[str]
    n: int = 0
    torque_sum: float = 0.0
    speed_sum: float = 0.0
    pressure_sum: float = 0.0
    grout_sum: float = 0.0
    torque_max: float = 0.0
    speed_min: float = float("inf")
    pressure_sum_sq: float = 0.0
    settlement: float = 0.0
    wear: float = 0.0
    water_risk: str = ""
    closed: bool = False
    breach_count: int = 0
    stable: bool = False
    close_t: int = 0
    stratum_votes: Dict[str, int] = field(default_factory=dict)
    band_snapshot: Optional[dict] = None

    def avg_pressure(self) -> float:
        return self.pressure_sum / self.n if self.n else 0.0
