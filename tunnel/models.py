"""隧道掘进施工剖面系统 —— 领域模型。

仅包含数据结构与少量纯计算辅助函数；业务流程位于 service.py。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple


# ---------------------------------------------------------------- 枚举

class Origin(str, Enum):
    """指令来源：人工 / 自动保护 / 自动优化。"""
    MANUAL = "manual"
    PROTECTION = "protection"
    AUTO_PLAN = "auto_plan"


class Ack(str, Enum):
    """指令仲裁结果。"""
    ACCEPTED = "accepted"          # 被采纳
    REJECTED_BY_PROTECTION = "rejected_by_protection"
    REJECTED_BY_EMERGENCY = "rejected_by_emergency"
    REJECTED_AS_SIMULATED = "rejected_as_simulated"
    SUPERSEDED_BY_SAFER = "superseded_by_safer"
    SKIPPED_AFTER_STOP = "skipped_after_stop"


class ScenarioRole(str, Enum):
    ACTIVE = "active"        # 绑定真实盾构机，指令下发现场
    WHATIF = "whatif"        # 假设方案，只推演不下发


class ReadStatus(str, Enum):
    ON_TIME = "on_time"
    LATE = "late"            # 迟到数据，已按事件时间重放
    QUARANTINED = "quarantined"  # 过旧 / 重复，隔离不处理


# ---------------------------------------------------------------- 剖面与地层

@dataclass
class StrataColumn:
    """某桩号处的地层柱状描述。"""
    chainage: float                 # 桩号 m
    code: str                       # 地层编码
    name: str                       # 地层名称
    cohesion: float                 # 黏聚力 kPa
    modulus: float                  # 变形模量 MPa
    permeability: float             # 渗透系数 m/d
    abrasivity: float               # 磨蚀性指数 0~1
    cover_depth: float              # 覆土厚度 m
    water_head: float               # 水头高度 m

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StrataColumn":
        return cls(**d)


@dataclass
class ConstructionProfile:
    """可操作施工剖面：一个预测区段上的掘进参数包络。

    bounds 为安全硬边界，profile 为当前目标设定值。
    """
    segment_id: str
    chainage_from: float
    chainage_to: float
    # 目标设定
    torque_kNm: float
    advance_speed_mm_min: float
    chamber_pressure_bar: float
    grout_m3_per_ring: float
    # 安全包络（min, max）
    torque_bounds: Tuple[float, float] = (0.0, 10_000.0)
    speed_bounds: Tuple[float, float] = (0.0, 120.0)
    pressure_bounds: Tuple[float, float] = (0.0, 4.0)
    grout_bounds: Tuple[float, float] = (0.0, 30.0)

    def contains(self, chainage: float) -> bool:
        return self.chainage_from <= chainage < self.chainage_to

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["torque_bounds"] = list(self.torque_bounds)
        d["speed_bounds"] = list(self.speed_bounds)
        d["pressure_bounds"] = list(self.pressure_bounds)
        d["grout_bounds"] = list(self.grout_bounds)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ConstructionProfile":
        d = dict(d)
        for key in ("torque_bounds", "speed_bounds", "pressure_bounds", "grout_bounds"):
            if key in d and not isinstance(d[key], tuple):
                d[key] = tuple(d[key])
        return cls(**d)


# ---------------------------------------------------------------- 传感数据

@dataclass
class Telemetry:
    """单环（或环内采样）传感读数。seq 单调递增，event_time_ms 为采集时刻。"""
    source_uid: str                 # 去重用，形如 t-42
    seq: int
    ring_no: int
    event_time_ms: int
    chainage: float
    torque_kNm: float
    advance_speed_mm_min: float
    chamber_pressure_bar: float
    grout_m3: float
    thrust_kN: float = 0.0
    received_ms: Optional[int] = None  # 入库时刻；event_time<水位线 => 迟到

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Telemetry":
        return cls(**d)


# ---------------------------------------------------------------- 风险与异常

@dataclass
class RingRisk:
    """单环推演结果（确定性物理启发式，便于替换为真实模型）。"""
    ring_no: int
    settlement_mm: float            # 地表沉降（槽谷中心，Peck 公式）
    wear_per_ring_mm: float         # 本环刀具磨损增量
    water_inrush_risk: float        # 涌水风险 0~1
    stability_index: float          # 综合稳定指数 0~1（越高越稳）
    detail: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FactorNode:
    """连锁原因链上的一个环节。"""
    factor: str                     # strata_change / torque / speed ...
    observed: float
    threshold: float
    direction: str                  # high / low / changed
    note: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AnomalyRecord:
    """异常：地层突变，或多项参数共同越界。"""
    anomaly_uid: str
    ring_no: int
    seq: int
    event_time_ms: int
    kinds: List[str]                # strata_change / joint_breach / critical
    breach_factors: List[str]
    factor_chain: List[Dict[str, Any]]  # 连锁原因链（含 cause/effect）
    before_snapshot: Dict[str, Any]
    after_snapshot: Dict[str, Any]
    retroactive: bool = False       # 是否由迟到数据追溯产生/修订
    resolved: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StateSnapshot:
    """异常前后保留的状态。"""
    ring_no: int
    seq: int
    event_time_ms: int
    chainage: float
    profile: Dict[str, Any]
    telemetry: Dict[str, Any]
    risk: Dict[str, Any]
    strata: Dict[str, Any]
    protections_active: Dict[str, bool]
    flags: Dict[str, bool]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StableNode:
    """历史稳定节点（可从此处分叉新方案）。"""
    label: str
    ring_no: int
    seq: int
    chainage: float
    profile: Dict[str, Any]
    risk: Dict[str, Any]
    strata_code: str
    consecutive_clean_rings: int
    recorded_ms: int
