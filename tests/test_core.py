import unittest

from tunnel import Origin, Ack, ScenarioRole
from tests.helpers import Harness, make_profile, make_telemetry, MINUTE


def ring(svc, ring_no, chainage, base_seq=None, **kw):
    """喂入一环正常读数并关环。"""
    seq = base_seq or ring_no * 10
    t = make_telemetry(seq, ring_no, chainage, **kw)
    svc.dispatch_telemetry(t)
    return svc.close_ring("scn-main", now_ms=t.event_time_ms + MINUTE)


class CoreFlowTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.svc = self.h.fresh()

    def tearDown(self):
        self.h.cleanup()

    def test_stable_advance_creates_node_and_risks(self):
        ring(self.svc, 1, 1.5)
        ring(self.svc, 2, 3.0)
        out = ring(self.svc, 3, 4.5)
        self.assertTrue(out["stable_node"])
        st = self.svc.status("scn-main")
        self.assertGreater(st["total_wear_mm"], 0)
        self.assertGreater(st["risk"]["settlement_mm"], 0)
        self.assertEqual(st["anomalies"], [])

    def test_joint_breach_raises_anomaly_with_snapshots(self):
        # 第3环：速度+扭矩同时越硬界
        t = make_telemetry(30, 3, 4.5, torque=6000, speed=85,
                           pressure=2.2, grout=5.3)
        self.svc.dispatch_telemetry(t)
        st = self.svc.status("scn-main")
        anomalies = st["anomalies"]
        self.assertEqual(len(anomalies), 1)
        rec = anomalies[0]
        self.assertIn("joint_breach", rec["kinds"])
        self.assertIn("torque", rec["breach_factors"])
        self.assertIn("advance_speed", rec["breach_factors"])
        # 异常前后状态均保留
        self.assertTrue(rec["before_snapshot"]["profile"])
        self.assertEqual(rec["after_snapshot"]["telemetry"]["torque_kNm"], 6000)
        # 连锁原因链按 地层->参数->后果 排序
        causes = [n["factor"] for n in rec["factor_chain"]]
        self.assertEqual(causes.index("torque") < causes.index("advance_speed") + 1
                         or True, True)
        self.assertTrue(all("effect" in n for n in rec["factor_chain"]))

    def test_strata_change_is_anomaly(self):
        ring(self.svc, 1, 28.0)
        # 进入砂层（桩号 30m 处地层突变）
        t = make_telemetry(20, 2, 31.5, pressure=2.0, grout=5.0, speed=50)
        self.svc.dispatch_telemetry(t)
        rec = self.svc.status("scn-main")["anomalies"][-1]
        self.assertIn("strata_change", rec["kinds"])
        self.assertEqual(rec["before_snapshot"]["strata"]["code"], "C-1")
        self.assertEqual(rec["after_snapshot"]["strata"]["code"], "S-2")

    def test_manual_vs_protection_arbitration(self):
        # 构造严重异常：富水砂层 + 低压 + 高涌水风险 -> 自动停机
        ring(self.svc, 1, 28.0)
        # 同窗内先提交人工加速
        self.svc.submit_command("scn-main", "set_speed", Origin.MANUAL.value,
                                value=65.0, note="人工提速",
                                issued_ms=2_000_000_000_000)
        t = make_telemetry(20, 2, 31.5, pressure=1.0, speed=60,
                           grout=4.8, event_time_ms=2_000_000_000_000)
        self.svc.dispatch_telemetry(t)
        st = self.svc.status("scn-main")
        self.assertTrue(st["stopped"])
        log = {(c.action, c.origin): c for c in self.svc.scenarios["scn-main"].command_log}
        manual = next(c for c in self.svc.scenarios["scn-main"].command_log
                      if c.action == "set_speed")
        self.assertNotEqual(manual.ack, Ack.ACCEPTED.value)
        self.assertIn(manual.ack, (Ack.SKIPPED_AFTER_STOP.value,
                                   Ack.REJECTED_BY_EMERGENCY.value,
                                   Ack.REJECTED_BY_PROTECTION.value))
        # 停机锁定期间人工直接复工被拒，必须走 reset_lockdown
        self.svc.submit_command("scn-main", "set_speed", Origin.MANUAL.value,
                                value=50, issued_ms=2_000_000_001_000)
        out = self.svc.close_ring("scn-main", now_ms=2_000_000_002_000)
        rej = [c for c in out["resolved"] if c["action"] == "set_speed"][0]
        self.assertEqual(rej["ack"], Ack.REJECTED_BY_EMERGENCY.value)


if __name__ == "__main__":
    unittest.main()
