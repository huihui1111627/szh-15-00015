"""测试公共夹具：地层剖面、读数构造、临时目录服务。"""
import os
import shutil
import tempfile

from tunnel import (
    EventStore, TunnelService, StrataColumn, ConstructionProfile, Telemetry,
    ScenarioRole,
)

MINUTE = 60_000


def make_strata():
    return [
        StrataColumn(0.0, "C-1", "粉质黏土", cohesion=42, modulus=18,
                     permeability=0.05, abrasivity=0.20,
                     cover_depth=14, water_head=6),
        StrataColumn(30.0, "S-2", "中粗砂(富水)", cohesion=6, modulus=28,
                     permeability=3.2, abrasivity=0.55,
                     cover_depth=15, water_head=9),
        StrataColumn(60.0, "R-3", "风化岩", cohesion=120, modulus=120,
                     permeability=0.02, abrasivity=0.85,
                     cover_depth=16, water_head=4),
    ]


def make_profile(seg="seg-1", frm=0.0, to=30.0):
    return ConstructionProfile(
        segment_id=seg, chainage_from=frm, chainage_to=to,
        torque_kNm=3200, advance_speed_mm_min=45,
        chamber_pressure_bar=2.3, grout_m3_per_ring=5.2,
        torque_bounds=(0.0, 5200), speed_bounds=(5.0, 70.0),
        pressure_bounds=(1.2, 3.2), grout_bounds=(4.5, 7.0))


def make_telemetry(seq, ring, chainage, *, torque=3100.0, speed=44.0,
                   pressure=2.28, grout=5.3, thrust=28000.0,
                   event_time_ms=None, uid=None):
    if event_time_ms is None:
        event_time_ms = 1_700_000_000_000 + ring * 10 * MINUTE + seq * MINUTE
    return Telemetry(
        source_uid=uid or f"t-{seq}", seq=seq, ring_no=ring,
        event_time_ms=event_time_ms, chainage=chainage,
        torque_kNm=torque, advance_speed_mm_min=speed,
        chamber_pressure_bar=pressure, grout_m3=grout, thrust_kN=thrust)


class Harness:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="tunnel-test-")
        self.session = "sess-test"

    def service(self):
        store = EventStore(self.root, self.session)
        svc = TunnelService(store, clock_ms=lambda: 1_700_100_000_000)
        svc.import_strata(make_strata())
        return svc

    def fresh(self):
        svc = self.service()
        svc.open_session(make_profile())
        return svc

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)
