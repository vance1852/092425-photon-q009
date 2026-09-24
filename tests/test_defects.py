from __future__ import annotations

import unittest

from photon_fab.defects import QualityGateError
from photon_fab.service import PhotonService


class DefectFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService(":memory:")
        self.service.bootstrap_admin()
        self.admin = self.service.auth.login("admin", "photon-admin")
        for user_id, role, password in (
            ("op", "operator", "operator1"),
            ("eng", "engineer", "engineer1"),
            ("qa", "quality", "quality-12"),
        ):
            self.service.auth.create_user(user_id, password, role)
        self.op = self.service.auth.login("op", "operator1")
        self.eng = self.service.auth.login("eng", "engineer1")
        self.qa = self.service.auth.login("qa", "quality-12")
        self.service.create_lot(self.eng, "LOT-1", "CMOS image sensor", "P3.2", 10)

    def _register_critical(self) -> str:
        defect = self.service.defects.register_defect(
            self.op, "LOT-1", "SCR-01", "封装表面划痕", "critical",
            "packaging-test", "eng", "目检发现划道，存在暗电流超限风险",
        )
        return defect["defect_id"]

    def test_critical_defect_holds_lot_and_blocks_release(self) -> None:
        defect_id = self._register_critical()
        self.assertEqual(self.service.get_lot(self.qa, "LOT-1")["status"], "hold")
        with self.assertRaises(QualityGateError):
            self.service.approve(self.qa, "LOT-1", "release", "尝试放行")
        # hold/reject 决定仍然允许
        self.service.approve(self.qa, "LOT-1", "hold", "等待返工")

    def test_full_rework_retest_close_release_flow(self) -> None:
        defect_id = self._register_critical()
        task = self.service.defects.dispatch_rework(
            self.eng, defect_id, "op", "重新抛光并目检", "按返工工艺卡 RW-2 处理")
        self.assertEqual(task["status"], "rework_open")
        task_id = task["rework_tasks"][-1]["task_id"]

        # 返工未完成不能复测
        with self.assertRaises(ValueError):
            self.service.defects.record_retest(self.op, defect_id, "pass", reason="提前复测")

        self.service.defects.complete_rework(self.op, task_id, "已重新抛光", "返工完成")
        # 返工完成但复测未通过，仍然禁止放行且批次保持 hold
        failed = self.service.defects.record_retest(
            self.op, defect_id, "fail", {"dark_current_na": 42}, "暗电流仍超限", "复测不合格")
        self.assertEqual(failed["status"], "retest_failed")
        with self.assertRaises(QualityGateError):
            self.service.approve(self.qa, "LOT-1", "release", "尝试放行")

        # 复测失败后重新派工 -> 完成 -> 复测通过
        task2 = self.service.defects.dispatch_rework(
            self.eng, defect_id, "op", "二次返工", "复测失败，重新处理")
        task2_id = task2["rework_tasks"][-1]["task_id"]
        self.service.defects.complete_rework(self.op, task2_id, "二次返工完成", "返工完成")
        passed = self.service.defects.record_retest(
            self.op, defect_id, "pass", {"dark_current_na": 3.1}, "暗电流合格", "复测合格")
        self.assertEqual(passed["status"], "retest_passed")

        # 复测通过但缺陷未关闭，仍不能放行
        with self.assertRaises(QualityGateError):
            self.service.approve(self.qa, "LOT-1", "release", "尝试放行")

        # 操作员不能关闭缺陷，必须质量人员
        with self.assertRaises(PermissionError):
            self.service.defects.close_defect(self.op, defect_id, "让步关闭")
        self.service.defects.close_defect(self.qa, defect_id, "复测通过，质量确认闭环")
        lot = self.service.approve(self.qa, "LOT-1", "release", "缺陷闭环，批准放行")
        self.assertEqual(lot["status"], "released")

    def test_severe_defect_requires_passed_retest_before_close(self) -> None:
        defect_id = self._register_critical()
        with self.assertRaises(QualityGateError):
            self.service.defects.close_defect(self.qa, defect_id, "直接让步接收")

    def test_minor_defect_does_not_block_release(self) -> None:
        self.service.defects.register_defect(
            self.op, "LOT-1", "COS-09", "标签轻微歪斜", "minor",
            "packaging-test", "op", "外观小瑕疵，不影响性能",
        )
        lot = self.service.approve(self.qa, "LOT-1", "release", "minor 不阻塞，放行")
        self.assertEqual(lot["status"], "released")

    def test_defect_registered_on_released_lot_recalls_to_hold(self) -> None:
        self.service.approve(self.qa, "LOT-1", "release", "初次放行")
        defect_id = self._register_critical()
        self.assertEqual(self.service.get_lot(self.qa, "LOT-1")["status"], "hold")
        with self.assertRaises(QualityGateError):
            self.service.approve(self.qa, "LOT-1", "release", "未闭环再次放行")

    def test_voided_defect_releases_gate(self) -> None:
        defect_id = self._register_critical()
        # 操作员不能作废
        with self.assertRaises(PermissionError):
            self.service.defects.void_defect(self.op, defect_id, "误登记")
        self.service.defects.void_defect(self.qa, defect_id, "复核为检测夹具反光造成的误报")
        lot = self.service.approve(self.qa, "LOT-1", "release", "缺陷作废，放行")
        self.assertEqual(lot["status"], "released")

    def test_every_transition_records_actor_and_reason(self) -> None:
        defect_id = self._register_critical()
        task = self.service.defects.dispatch_rework(
            self.eng, defect_id, "op", "重新抛光", "派工原因")
        task_id = task["rework_tasks"][-1]["task_id"]
        self.service.defects.complete_rework(self.op, task_id, "完成", "返工完工原因")
        self.service.defects.record_retest(self.op, defect_id, "pass", {}, "数据正常", "复测通过原因")
        self.service.defects.close_defect(self.qa, defect_id, "关闭原因")

        history = self.service.defects.history(self.qa, defect_id)
        transitions = [(e["event_type"], e["actor"], e["reason"], e["from_status"], e["to_status"])
                       for e in history]
        self.assertEqual(transitions, [
            ("registered", "op", "目检发现划道，存在暗电流超限风险", None, "open"),
            ("rework_dispatched", "eng", "派工原因", "open", "rework_open"),
            ("rework_completed", "op", "返工完工原因", "rework_open", "reworked"),
            ("retest_pass", "op", "复测通过原因", "reworked", "retest_passed"),
            ("closed", "qa", "关闭原因", "retest_passed", "closed"),
        ])
        # 批次审计流同样保留操作者
        lot_events = self.service.audit(self.qa, "LOT-1")
        self.assertIn("defect_hold", [e["event_type"] for e in lot_events])

    def test_retest_is_linked_to_rework_task(self) -> None:
        defect_id = self._register_critical()
        task = self.service.defects.dispatch_rework(self.eng, defect_id, "op", "返工", "派工")
        task_id = task["rework_tasks"][-1]["task_id"]
        self.service.defects.complete_rework(self.op, task_id, "完工", "返工完成")
        result = self.service.defects.record_retest(
            self.op, defect_id, "pass", {"noise_rms": 0.02}, "复测", "复测通过")
        self.assertEqual(result["retests"][0]["task_id"], task_id)
        self.assertEqual(result["retests"][0]["tested_by"], "op")


if __name__ == "__main__":
    unittest.main()
