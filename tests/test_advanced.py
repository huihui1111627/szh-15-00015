import unittest

from tunnel import Origin, Ack, ScenarioRole, TunnelService
from tests.helpers import Harness, make_profile, make_telemetry, MINUTE


class AdvancedTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.svc = self.h.fresh()

    def tearDown(self):
        self.h.cleanup()

    def _advance_clean_rings(self, n=3):
        for r in range(1, n + 1):
            t = make_telemetry(r * 10, r, r * 1.5)
            self.svc.dispatch_telemetry(t)
            self.svc.close_ring("scn-main", now_ms=t.event_time_ms + MINUTE)

    # ---------------------------------------------------------- 迟到数据
    def test_late_telemetry_retroactive_and_duplicate(self):
        self._advance_clean_rings(2)
        # 第3环正常读数（但压力偏低一项不构成异常）
        t3 = make_telemetry(30, 3, 4.5, pressure=1.9)
        self.svc.dispatch_telemetry(t3)
        self.svc.close_ring("scn-main", now_ms=t3.event_time_ms + MINUTE)
        anomalies_before = len(self.svc.status("scn-main")["anomalies"])

        # 迟到的同环读数：扭矩+速度同时越界 -> 追溯异常，带 retroactive
        late = make_telemetry(31, 3, 4.5, torque=6000, speed=85,
                              event_time_ms=t3.event_time_ms - 30_000)
        status = self.svc.ingest_telemetry("scn-main", late)
        self.assertEqual(status, "late")
        rec = self.svc.status("scn-main")["anomalies"][-1]
        self.assertTrue(rec["retroactive"])
        self.assertGreater(len(self.svc.status("scn-main")["anomalies"]),
                           anomalies_before)

        # 重复读数被隔离
        again = self.svc.ingest_telemetry("scn-main", late)
        self.assertEqual(again, "quarantined")
        reasons = [q["reason"] for q in self.svc.status("scn-main")["quarantined"]]
        self.assertIn("duplicate", reasons)

    def test_beyond_tolerance_is_quarantined(self):
        self._advance_clean_rings(3)
        very_late = make_telemetry(5, 1, 1.5, torque=9999,
                                   event_time_ms=1_700_000_000_000)
        self.assertEqual(self.svc.ingest_telemetry("scn-main", very_late),
                         "quarantined")

    def test_ring_gap_is_quarantined_until_filled(self):
        t1 = make_telemetry(10, 1, 1.5)
        self.svc.dispatch_telemetry(t1)
        self.svc.close_ring("scn-main", now_ms=t1.event_time_ms + 60_000)
        # 直接跳到第 3 环（缺第 2 环）应隔离
        gap = make_telemetry(30, 3, 4.5)
        self.assertEqual(self.svc.ingest_telemetry("scn-main", gap),
                         "quarantined")
        reasons = [q["reason"] for q in self.svc.status("scn-main")["quarantined"]]
        self.assertTrue(any("ring_gap" in r for r in reasons))

    # ---------------------------------------------------------- 多方案
    def test_fork_from_stable_node_runs_in_parallel(self):
        self._advance_clean_rings(3)
        node = self.svc.status("scn-main")["stable_nodes"][-1]["label"]

        aggressive = make_profile()
        aggressive.advance_speed_mm_min = 60
        forked = self.svc.fork_from_stable_node(
            "scn-main", node, "scn-fast", "提速方案", profile=aggressive)
        self.assertEqual(forked.role, ScenarioRole.WHATIF.value)
        # 分叉方案从第3环位置接续
        self.assertEqual(forked.ring_no, 3)
        self.assertEqual(forked.profile.advance_speed_mm_min, 60)
        # 主控仍被引用
        self.assertIn("scn-fast", self.svc.status("scn-main")["forks"])

        # 第4环读数扇出，两个方案同步推进
        t4 = make_telemetry(40, 4, 6.0, speed=44)
        result = self.svc.dispatch_telemetry(t4)
        self.assertEqual(set(result), {"scn-main", "scn-fast"})

        # whatif 指令不下发现场（simulated），不影响主控
        self.svc.submit_command("scn-fast", "set_speed",
                                Origin.MANUAL.value, value=66)
        out = self.svc.close_ring("scn-fast", now_ms=t4.event_time_ms + MINUTE)
        sim = [c for c in out["resolved"] if c["action"] == "set_speed"][0]
        self.assertTrue(sim["simulated"])
        self.assertEqual(sim["ack"], Ack.ACCEPTED.value)
        self.svc.close_ring("scn-main", now_ms=t4.event_time_ms + MINUTE)
        self.assertNotEqual(
            self.svc.status("scn-main")["profile"]["advance_speed_mm_min"], 66)

        # 提升为主控：原主控降级
        self.svc.promote_scenario("scn-fast")
        self.assertEqual(self.svc.status("scn-fast")["role"], ScenarioRole.ACTIVE.value)
        self.assertEqual(self.svc.status("scn-main")["role"], ScenarioRole.WHATIF.value)

    # ---------------------------------------------------------- 重启接续
    def test_restart_reconstructs_state_and_pending_commands(self):
        self._advance_clean_rings(3)
        t4 = make_telemetry(40, 4, 6.0)
        self.svc.dispatch_telemetry(t4)
        # 未关环的未决人工指令
        self.svc.submit_command("scn-main", "set_grout", Origin.MANUAL.value,
                                value=6.1, note="加大注浆")
        wear_before = self.svc.status("scn-main")["total_wear_mm"]

        svc2 = TunnelService.restart(self.h.root, self.h.session)
        st = svc2.status("scn-main")
        self.assertEqual(st["ring_no"], 4)
        self.assertAlmostEqual(st["total_wear_mm"], wear_before, places=3)
        self.assertEqual(len(st["stable_nodes"]), 1)
        self.assertEqual(len(st["anomalies"]), 0)
        # 未决指令仍在，参与下一窗口仲裁并生效
        pending = st["pending_commands"]
        self.assertEqual(len(pending), 1)
        out = svc2.close_ring("scn-main", now_ms=t4.event_time_ms + MINUTE)
        self.assertEqual(out["resolved"][0]["ack"], Ack.ACCEPTED.value)
        self.assertEqual(svc2.status("scn-main")["profile"]["grout_m3_per_ring"], 6.1)

    def test_restart_restores_forked_scenarios_and_anomaly(self):
        self._advance_clean_rings(3)
        node = self.svc.status("scn-main")["stable_nodes"][-1]["label"]
        self.svc.fork_from_stable_node("scn-main", node, "scn-b", "B方案")
        bad = make_telemetry(40, 4, 6.0, torque=6000, speed=90)
        self.svc.dispatch_telemetry(bad)

        svc2 = TunnelService.restart(self.h.root, self.h.session)
        self.assertEqual(set(svc2.scenarios), {"scn-main", "scn-b"})
        self.assertEqual(svc2.status("scn-b")["forked_from"], "scn-main")
        self.assertTrue(svc2.status("scn-main")["anomalies"])

    # ---------------------------------------------------------- 预测区段
    def test_preview_segment_compares_profiles(self):
        conservative = make_profile("seg-sand", 30, 60)
        conservative.chamber_pressure_bar = 2.6
        conservative.advance_speed_mm_min = 30
        aggressive = make_profile("seg-sand", 30, 60)
        aggressive.chamber_pressure_bar = 0.5
        aggressive.advance_speed_mm_min = 65
        r_safe = self.svc.preview_segment(conservative, 31.5)
        r_risk = self.svc.preview_segment(aggressive, 31.5)
        self.assertIsNotNone(r_safe)
        self.assertLess(r_safe.water_inrush_risk, r_risk.water_inrush_risk)
        self.assertLess(r_safe.settlement_mm, r_risk.settlement_mm)


if __name__ == "__main__":
    unittest.main()
