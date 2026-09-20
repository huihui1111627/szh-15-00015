"""确定性的线路推进仿真器：沿预测区段生成传感读数并可注入异常。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .models import DEFAULT_STRATA, Reading, Segment


@dataclass
class FaultSpec:
    """在指定时间区间对读数施加偏移，模拟地层突变/设备异常。"""

    start: int
    end: int
    stratum: Optional[str] = None
    torque_add: float = 0.0
    speed_add: float = 0.0
    pressure_add: float = 0.0
    grout_add: float = 0.0


class LineSimulator:
    def __init__(self, forecast: List[Segment], readings_per_ring: int = 4,
                 minutes_per_reading: int = 5, chainage_per_reading: float = 0.375,
                 seed: int = 7):
        self.forecast = forecast
        self.readings_per_ring = readings_per_ring
        self.minutes_per_reading = minutes_per_reading
        self.chainage_per_reading = chainage_per_reading
        self.seed = seed

    def _jitter(self, step: int, axis: int) -> float:
        value = (self.seed * 1103515245 + step * 22699 + axis * 3089) & 0x7FFFFFFF
        return ((value % 1000) / 1000.0 - 0.5)

    def stratum_at(self, chainage: float) -> Optional[str]:
        for segment in self.forecast:
            if segment.covers(chainage):
                return segment.stratum_code
        return self.forecast[-1].stratum_code if self.forecast else None

    def generate(self, n_readings: int,
                 faults: Optional[List[FaultSpec]] = None,
                 overrides: Optional[Dict[str, Dict[str, tuple]]] = None,
                 source: str = "plc", t_offset: int = 0,
                 step_offset: int = 0) -> List[Reading]:
        faults = faults or []
        overrides = overrides or {}
        readings: List[Reading] = []
        for k in range(n_readings):
            step = k + step_offset
            t = step * self.minutes_per_reading + t_offset
            chainage = round(step * self.chainage_per_reading, 3)
            ring = 1 + step // self.readings_per_ring
            code = self.stratum_at(chainage) or "clay"
            stratum = DEFAULT_STRATA[code]

            def mid(bounds):
                return (bounds[0] + bounds[1]) / 2.0

            torque = mid(stratum.torque) + self._jitter(step, 0) * 120
            speed = mid(stratum.advance_speed) + self._jitter(step, 1) * 3
            pressure = mid(stratum.chamber_pressure) + self._jitter(step, 2) * 0.08
            grout = mid(stratum.grout_volume) + self._jitter(step, 3) * 0.2

            observed: Optional[str] = None
            for fault in faults:
                if fault.start <= t <= fault.end:
                    torque += fault.torque_add
                    speed += fault.speed_add
                    pressure += fault.pressure_add
                    grout += fault.grout_add
                    if fault.stratum:
                        observed = fault.stratum
            readings.append(Reading(
                t=t, ring=ring, chainage=chainage,
                torque=round(torque, 1), advance_speed=round(speed, 2),
                chamber_pressure=round(pressure, 3), grout_volume=round(grout, 3),
                observed_stratum=observed, source=source,
            ))
        return readings
