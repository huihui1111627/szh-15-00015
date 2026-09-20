"""施工剖面控制系统的能力测试（标准库 unittest，无外部依赖）。"""
import os
import tempfile
import unittest

from tunnel_profile import Engine, EventStore, Reading, Segment
from tunnel_profile.models import Band
from tunnel_profile.rules import ring_risks


FORECAST = [
    Segment("S1", 0.0, 10.0, "clay"),
    Segment("S2", 10.0, 20.0, "sand"),
]


def normal_reading(t: int, ring: int, chainage: float,
                   stratum: str = None) -> Reading:
    return Reading(t=t, ring=ring, chainage=chainage,
                   torque=2500, advance_speed=40,
                   chamber_pressure=2.1, grout_volume=6.2,
                   observed_stratum=stratum)


def clay_drive(n_steps: int, t0: int = 0):
    """生成始终位于黏土预测区段内的读数（里程上限 9m）。"""
    return [
        normal_reading(t0 + 5 * i, 1 + i // 4, min(1.5 * i, 9.0))
        for i in range(n_steps)
    ]


class TunnelProfileTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(EventStore())
        self.engine.create_scenario("main", FORECAST, {"S2": 20.0})

    def push(self, *readings):
        for r in readings:
            self.engine.submit_reading("main", r)
        return self.engine.states["main"]

    def test_risk_models(self):
        band = Band((3000, 4600), (15, 35), (2.6, 3.8), (7.0, 9.5))
        from tunnel_profile.models import DEFAULT_STRATA
        sand = DEFAULT_STRATA["sand"]
        settlement, wear, water = ring_risks(
            4, torque_sum=14000, torque_max=3800,
            speed_sum=100, speed_min=22, pressure_sum=10.0,
            grout_sum=30.0, band=band, stratum=sand, water_head_m=20.0,
        )
        self.assertGreater(settlement, 1.0)       # 欠压 -> 沉降
        self.assertGreater(wear, 0.5)
        self.assertIn(water, ("高", "极高"))       # k*h = 1e-4*20

    def test_ring_closure_and_risks_update(self):
        state = self.push(*clay_drive(8))
        self.assertTrue(state.rings[1].closed)
        self.assertGreater(state.rings[1].settlement, 0.0)
        self.assertGreaterEqual(state.settlement_total, state.rings[1].settlement)
        self.assertIn(1, [n["ring"] for n in state.stable_nodes])

    def test_manual_adjust_changes_band(self):
        state, decision = self.engine.adjust(
            "main", 0, 1, "李工",
            (2000, 3000), (30, 40), (2.0, 2.8), (5.5, 7.0),
        )
        self.assertEqual(decision["outcome"], "accepted")
        band = state.band_for("clay")
        self.assertEqual(band.advance_speed, (30, 40))

    def test_joint_breach_records_before_after_and_chain(self):
        state = self.push(
            normal_reading(0, 1, 0.0),
            Reading(5, 1, 1.5, torque=5200, advance_speed=8,
                    chamber_pressure=1.0, grout_volume=4.0,
                    observed_stratum="gravel"),
        )
        incident = state.incidents["INC-R001-joint_breach"]
        self.assertGreaterEqual(len(incident.metrics), 2)
        self.assertIn("chamber_pressure", incident.before)
        self.assertIn("chamber_pressure", incident.after)
        self.assertGreater(len(incident.chain), 2)
        joined = incident.chain_text()
        self.assertIn("多项参数越界", joined)
        self.assertIn("自动保护联锁", joined)

    def test_stratum_change_incident(self):
        state = self.push(
            normal_reading(0, 1, 0.0, stratum="clay"),
            Reading(5, 1, 1.5, torque=4000, advance_speed=20,
                    chamber_pressure=3.2, grout_volume=8.5,
                    observed_stratum="gravel"),
        )
        kinds = {i.kind for i in state.incidents.values()}
        self.assertIn("stratum_change", kinds)
        change = next(i for i in state.incidents.values()
                      if i.kind == "stratum_change")
        self.assertEqual(change.before["stratum"], "clay")
        self.assertEqual(change.after["stratum"], "gravel")

    def test_protection_suppresses_manual_until_safe_and_ack(self):
        self.push(
            normal_reading(0, 1, 0.0),
            Reading(5, 1, 1.5, torque=5200, advance_speed=8,
                    chamber_pressure=1.0, grout_volume=4.0,
                    observed_stratum="gravel"),
        )
        _, decision = self.engine.adjust(
            "main", 6, 1, "王班长",
            (4000, 5500), (20, 30), (3.2, 4.0), (8.5, 10.5),
        )
        self.assertEqual(decision["outcome"], "suppressed")

        for t in (10, 15, 20):
            self.push(Reading(t, 1 + t // 20, t * 1.5 / 5,
                              torque=2600, advance_speed=40,
                              chamber_pressure=2.1, grout_volume=6.2))
        # 只确认但安全读数不足时仍锁定
        self.engine.acknowledge_protection("main", 12, "王班长")
        state = self.engine.states["main"]
        triggered = list(state.protections)
        self.assertTrue(triggered)

    def test_protection_clears_after_three_safe_plus_ack(self):
        self.push(
            normal_reading(0, 1, 0.0),
            Reading(5, 1, 1.5, torque=5200, advance_speed=8,
                    chamber_pressure=1.0, grout_volume=4.0,
                    observed_stratum="gravel"),
        )
        for t in (10, 15):
            self.push(normal_reading(t, 1, 0.0 + t * 0.3))
        self.engine.acknowledge_protection("main", 15, "王班长")
        self.push(normal_reading(20, 2, 3.0))
        self.assertEqual(self.engine.states["main"].active_protection_names(), [])

    def test_force_override(self):
        self.push(
            normal_reading(0, 1, 0.0),
            Reading(5, 1, 1.5, torque=5200, advance_speed=8,
                    chamber_pressure=1.0, grout_volume=4.0,
                    observed_stratum="gravel"),
        )
        _, decision = self.engine.adjust(
            "main", 6, 1, "王班长",
            (4000, 5500), (20, 30), (3.2, 4.0), (8.5, 10.5),
            force=True,
        )
        self.assertEqual(decision["outcome"], "forced_override")
        self.assertEqual(self.engine.states["main"].active_protection_names(), [])

    def test_late_reading_within_window_amends(self):
        self.push(*clay_drive(12))
        state, note = self.engine.submit_reading(
            "main",
            Reading(52, 3, 9.0, torque=4300, advance_speed=45,
                    chamber_pressure=1.4, grout_volume=5.5,
                    source="backfill"),
        )
        self.assertIn("窗口内", note)
        self.assertTrue(state.incidents["INC-R003-joint_breach"].amended)

    def test_late_reading_outside_window_archived_only(self):
        self.push(*clay_drive(40))
        n_before = self.engine.states["main"].rings[1].n
        state, note = self.engine.submit_reading(
            "main",
            Reading(0, 1, 0.0, torque=5000, advance_speed=5,
                    chamber_pressure=0.5, grout_volume=1.0),
        )
        self.assertIn("仅入档", note)
        self.assertEqual(state.rings[1].n, n_before)

    def test_clone_from_stable_node_is_independent(self):
        self.push(*clay_drive(8))
        clone = self.engine.clone_stable("main", "conservative", t=100,
                                         up_to_ring=1)
        self.assertEqual(clone.created_from, "main")
        self.assertEqual(clone.rings[1].n, 4)
        self.engine.adjust(
            "conservative", 100, 2, "赵总工",
            (1800, 2800), (30, 38), (2.0, 2.6), (5.5, 7.0),
        )
        main_band = self.engine.states["main"].band_for("clay")
        clone_band = self.engine.states["conservative"].band_for("clay")
        self.assertNotEqual(main_band.torque, clone_band.torque)

    def test_clone_requires_stable_node(self):
        with self.assertRaises(ValueError):
            self.engine.clone_stable("main", "x", 10, up_to_ring=99)

    def test_restart_reconstructs_state_from_log(self):
        path = tempfile.mktemp(suffix=".jsonl")
        engine = Engine(EventStore(path))
        engine.create_scenario("main", FORECAST)
        for reading in clay_drive(8):
            engine.submit_reading("main", reading)
        # 调整粉土段参数带（t=24 已进入 silt 预测区段）
        engine.adjust("main", 24, 2, "李工",
                      (2000, 3000), (30, 40), (2.0, 2.8), (5.5, 7.0),
                      stratum="clay")

        restarted = Engine(EventStore(path))
        state = restarted.states["main"]
        self.assertTrue(state.rings[1].closed)
        self.assertEqual(state.band_for("clay").advance_speed, (30, 40))
        self.assertIn(1, [n["ring"] for n in state.stable_nodes])

    def test_duplicate_reading_rejected(self):
        self.push(normal_reading(0, 1, 0.0))
        _, note = self.engine.submit_reading("main", normal_reading(0, 1, 0.0))
        self.assertIn("重复", note)


if __name__ == "__main__":
    unittest.main()
