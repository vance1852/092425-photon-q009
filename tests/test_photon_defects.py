from __future__ import annotations

import unittest

from photon_fab.service import PhotonService


class DefectLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        self.admin = self.service.auth.login("admin", "photon-admin")
        self.service.auth.create_user("quality", "quality-pass", "quality")
        self.service.auth.create_user("operator", "operator-pass", "operator")
        self.service.auth.create_user("engineer", "engineer-pass", "engineer")
        self.quality = self.service.auth.login("quality", "quality-pass")
        self.operator = self.service.auth.login("operator", "operator-pass")
        self.engineer = self.service.auth.login("engineer", "engineer-pass")
        self.service.create_lot(self.engineer, "LOT-1", "PD array", "P1.0", 4)
        for w, r in ((450, .81), (520, .95), (650, .83)):
            self.service.add_measurement(self.operator, "LOT-1", w, r, .01, "spec-1")

    def _critical_defect(self, lot="LOT-1"):
        return self.service.register_defect(
            self.quality, lot, "scratch", "封装表面划痕", "critical",
            "芯片表面可见划痕", owner="engineer", reason="封装终检发现划痕")

    def test_critical_defect_holds_lot_and_blocks_release(self) -> None:
        defect = self._critical_defect()
        self.assertEqual(defect["status"], "open")
        self.assertEqual(self.service.get_lot(self.quality, "LOT-1")["status"], "hold")
        with self.assertRaises(PermissionError):
            self.service.approve(self.quality, "LOT-1", "release", "试图放行")

    def test_major_defect_blocks_but_minor_does_not(self) -> None:
        major = self.service.register_defect(
            self.quality, "LOT-1", "dark_current", "暗电流超限", "major",
            "9nA/8nA", owner="engineer", reason="测试超限")
        with self.assertRaises(PermissionError):
            self.service.approve(self.quality, "LOT-1", "release", "放行")
        task = self.service.create_rework_task(
            self.operator, major["defect_id"], "更换偏置电阻", "engineer", "评审返工")
        self.service.complete_rework_task(self.operator, task["task_id"], "已更换")
        self.service.record_retest(self.quality, major["defect_id"], "pass", "7nA 合格")

        minor = self.service.register_defect(
            self.quality, "LOT-1", "cosmetic", "标签轻微歪斜", "minor",
            "不影响性能", owner="operator", reason="外观记录")
        # minor 未关闭也不阻止放行（严重缺陷才阻止）
        lot = self.service.approve(self.quality, "LOT-1", "release", "major 已闭环")
        self.assertEqual(lot["status"], "released")

    def test_full_rework_retest_release_flow(self) -> None:
        defect = self._critical_defect()
        with self.assertRaises(ValueError):
            # 返工未开始时不允许复测
            self.service.record_retest(self.quality, defect["defect_id"], "pass", "提前复测")

        task = self.service.create_rework_task(
            self.operator, defect["defect_id"], "重新清洗并复检封装", "engineer",
            reason="工程决定返工清洗")
        self.assertEqual(self.service.get_defect(self.quality, defect["defect_id"])["status"], "reworking")

        # 返工未完成前批次仍 hold，无法放行
        with self.assertRaises(PermissionError):
            self.service.approve(self.quality, "LOT-1", "release", "返工中放行")

        self.service.complete_rework_task(self.operator, task["task_id"], "清洗完成，待复测")
        self.assertEqual(self.service.get_defect(self.quality, defect["defect_id"])["status"],
                         "retest_pending")

        failed = self.service.record_retest(
            self.quality, defect["defect_id"], "fail", "划痕仍可见")
        self.assertEqual(failed["result"], "fail")
        self.assertEqual(self.service.get_defect(self.quality, defect["defect_id"])["status"], "open")
        self.assertEqual(self.service.get_lot(self.quality, "LOT-1")["status"], "hold")

        # 复测失败后重新返工
        task2 = self.service.create_rework_task(
            self.operator, defect["defect_id"], "整片重新封装", "engineer", reason="首次返工无效")
        self.service.complete_rework_task(self.operator, task2["task_id"], "重新封装完成")
        self.service.record_retest(self.quality, defect["defect_id"], "pass", "无划痕")
        self.assertEqual(self.service.get_defect(self.quality, defect["defect_id"])["status"], "closed")

        lot = self.service.approve(self.quality, "LOT-1", "release", "缺陷闭环，复测合格")
        self.assertEqual(lot["status"], "released")

    def test_multiple_rework_tasks_must_all_finish_before_retest(self) -> None:
        defect = self._critical_defect()
        t1 = self.service.create_rework_task(self.operator, defect["defect_id"], "任务一", "engineer", "原因一")
        self.service.create_rework_task(self.operator, defect["defect_id"], "任务二", "engineer", "原因二")
        self.service.complete_rework_task(self.operator, t1["task_id"], "任务一完成")
        # 还有一个任务未完成：不进入待复测
        self.assertEqual(self.service.get_defect(self.quality, defect["defect_id"])["status"], "reworking")
        with self.assertRaises(ValueError):
            self.service.record_retest(self.quality, defect["defect_id"], "pass", "复测")

    def test_retest_can_link_measurement(self) -> None:
        defect = self._critical_defect()
        task = self.service.create_rework_task(self.operator, defect["defect_id"], "返工", "engineer", "原因")
        self.service.complete_rework_task(self.operator, task["task_id"], "完成")
        with self.assertRaises(ValueError):
            self.service.record_retest(
                self.quality, defect["defect_id"], "pass", "关联不存在的测量",
                measurement_id="unknown")
        measurement = self.service.add_measurement(self.operator, "LOT-1", 520, .96, .008, "spec-2")
        retest = self.service.record_retest(
            self.quality, defect["defect_id"], "pass", "以新测量为准",
            measurement_id=measurement["measurement_id"])
        stored = self.service.get_defect(self.quality, defect["defect_id"])
        self.assertEqual(stored["retests"][0]["retest_id"], retest["retest_id"])
        self.assertEqual(stored["retests"][0]["measurement_id"], measurement["measurement_id"])

    def test_critical_cannot_close_without_passing_retest(self) -> None:
        defect = self._critical_defect()
        with self.assertRaises(PermissionError):
            self.service.close_minor_defect(self.quality, defect["defect_id"], "口头确认关闭")
        task = self.service.create_rework_task(self.operator, defect["defect_id"], "返工", "engineer", "原因")
        self.service.complete_rework_task(self.operator, task["task_id"], "完成")
        with self.assertRaises(PermissionError):
            self.service.close_minor_defect(self.quality, defect["defect_id"], "仍要求复测")

    def test_minor_defect_can_be_closed_directly_by_quality(self) -> None:
        minor = self.service.register_defect(
            self.quality, "LOT-1", "label", "标签歪斜", "minor",
            "外观", owner="operator", reason="记录")
        closed = self.service.close_minor_defect(self.quality, minor["defect_id"], "质量确认不影响放行")
        self.assertEqual(closed["status"], "closed")
        with self.assertRaises(PermissionError):
            # 操作员没有审批权，不能直接关闭
            self.service.close_minor_defect(self.operator, minor["defect_id"], "操作员尝试")

    def test_every_transition_records_actor_and_reason(self) -> None:
        defect = self._critical_defect()
        task = self.service.create_rework_task(self.operator, defect["defect_id"], "返工", "engineer", "返工原因")
        self.service.complete_rework_task(self.operator, task["task_id"], "完成原因")
        self.service.record_retest(self.quality, defect["defect_id"], "pass", "复测原因")
        history = self.service.defect_history(self.quality, defect["defect_id"])
        transitions = [(e["action"], e["from_status"], e["to_status"], e["actor"], e["reason"]) for e in history]
        self.assertEqual(transitions, [
            ("registered", None, "open", "quality", "封装终检发现划痕"),
            ("rework_created", "open", "reworking", "operator", "返工原因"),
            ("rework_completed", "reworking", "retest_pending", "operator", "完成原因"),
            ("retest_passed", "retest_pending", "closed", "quality", "复测原因"),
        ])
        # 批次审计也保留 hold 事件
        lot_events = self.service.audit(self.quality, "LOT-1")
        self.assertTrue(any(e["event_type"] == "quality_hold" and e["actor"] == "quality" for e in lot_events))

    def test_operator_cannot_register_without_credentials_or_invalid_severity(self) -> None:
        with self.assertRaises(ValueError):
            self.service.register_defect(
                self.quality, "LOT-1", "x", "致命级别写错", "fatal", "d", "engineer", "原因")
        with self.assertRaises(ValueError):
            self.service.register_defect(
                self.quality, "LOT-1", "x", "缺原因", "minor", "d", "engineer", "  ")

    def test_rejected_lot_not_reopened_by_defect(self) -> None:
        self.service.approve(self.quality, "LOT-1", "reject", "整批报废")
        self._critical_defect()
        self.assertEqual(self.service.get_lot(self.quality, "LOT-1")["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
