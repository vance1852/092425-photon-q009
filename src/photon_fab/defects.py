"""封装测试缺陷登记、返工、复测与放行门禁。

状态机::

    open ──dispatch──▶ rework_open ──complete──▶ reworked
      ▲                    │                         │
      │                    │              retest pass│retest fail
      │                    └───── (fail 后重新派工) ◀─┘
      │                                              ▼
      │                                       retest_failed
      │
      ├─ minor 且质量让步接收 ─▶ closed
      └─ 误登记 ─▶ void

    retest_passed ──质量关闭──▶ closed

major/critical 为严重（阻塞）缺陷：登记即冻结批次为 hold（已放行批次召回
hold）；只有复测通过并由质量人员关闭后，批次审批才允许选择 release。
minor 缺陷不阻塞放行，但必须由质量人员给出闭环处置。
"""

from __future__ import annotations

import json
import uuid

from .storage import (
    BLOCKING_SEVERITIES,
    DEFECT_STATUSES,
    SEVERITIES,
    defect_event,
    event,
    transaction,
    utcnow,
)


class QualityGateError(ValueError):
    """放行条件不满足（存在未闭环的严重缺陷等）。"""


class DefectTracker:
    def __init__(self, db, auth):
        self.db = db
        self.auth = auth

    # ---------------------------------------------------------------- 查询

    def get_defect(self, token: str, defect_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM defects WHERE defect_id=?", (defect_id,)).fetchone()
        if not row:
            raise KeyError(defect_id)
        defect = dict(row)
        defect["rework_tasks"] = [
            dict(r) for r in self.db.execute(
                "SELECT * FROM rework_tasks WHERE defect_id=? ORDER BY created_at", (defect_id,)
            ).fetchall()
        ]
        defect["retests"] = [
            dict(r) for r in self.db.execute(
                "SELECT * FROM retests WHERE defect_id=? ORDER BY tested_at", (defect_id,)).fetchall()
        ]
        return defect

    def list_defects(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM defects WHERE lot_id=? ORDER BY created_at", (lot_id,)).fetchall()]

    def history(self, token: str, defect_id: str) -> list[dict]:
        self.auth.require(token, "read")
        if not self.db.execute("SELECT 1 FROM defects WHERE defect_id=?", (defect_id,)).fetchone():
            raise KeyError(defect_id)
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM defect_events WHERE defect_id=? ORDER BY event_id", (defect_id,)).fetchall()]

    def open_blocking(self, lot_id: str) -> list[dict]:
        """返回仍阻塞放行的严重缺陷（未关闭/未作废）。"""
        placeholders = ",".join("?" for _ in BLOCKING_SEVERITIES)
        rows = self.db.execute(
            f"SELECT * FROM defects WHERE lot_id=? AND severity IN ({placeholders})"
            " AND status NOT IN ('closed','void') ORDER BY created_at",
            (lot_id, *BLOCKING_SEVERITIES),
        ).fetchall()
        return [dict(r) for r in rows]

    def assert_release_allowed(self, lot_id: str) -> None:
        blocking = self.open_blocking(lot_id)
        if blocking:
            ids = ", ".join(d["defect_id"] for d in blocking)
            raise QualityGateError(
                f"release blocked by {len(blocking)} unresolved critical defect(s): {ids}"
            )

    # ---------------------------------------------------------------- 登记

    def register_defect(self, token: str, lot_id: str, code: str, title: str,
                        severity: str, source: str, owner: str, reason: str) -> dict:
        actor = self.auth.require(token, "defect_register")
        if severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        if not code.strip() or not title.strip() or not owner.strip() or not reason.strip():
            raise ValueError("code, title, owner and reason are required")
        defect_id = uuid.uuid4().hex
        now = utcnow()
        with transaction(self.db):
            lot = self.db.execute("SELECT status FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if not lot:
                raise KeyError(lot_id)
            self.db.execute(
                "INSERT INTO defects VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (defect_id, lot_id, code.strip(), title.strip(), severity,
                 source.strip(), owner.strip(), "open", actor.user_id, now, now),
            )
            defect_event(self.db, defect_id, lot_id, "registered", actor.user_id, reason,
                         {"code": code, "severity": severity, "owner": owner, "source": source},
                         None, "open")
            event(self.db, lot_id, "defect_registered", actor.user_id,
                  {"defect_id": defect_id, "severity": severity, "reason": reason})
            # 严重缺陷立即冻结批次；已放行批次召回 hold，等待返工与复测。
            # 已 reject 的批次不再改状态（其本身已禁止放行）。
            if severity in BLOCKING_SEVERITIES and lot["status"] not in ("hold", "rejected"):
                self._set_lot_hold(lot_id, actor.user_id, defect_id,
                                   "recalled" if lot["status"] == "released" else "held")
        return self.get_defect(token, defect_id)

    # ---------------------------------------------------------------- 返工

    def dispatch_rework(self, token: str, defect_id: str, assignee: str,
                        instruction: str, reason: str) -> dict:
        actor = self.auth.require(token, "defect_dispatch")
        if not assignee.strip() or not instruction.strip() or not reason.strip():
            raise ValueError("assignee, instruction and reason are required")
        task_id = uuid.uuid4().hex
        with transaction(self.db):
            defect = self._locked(defect_id)
            if defect["status"] not in ("open", "retest_failed"):
                raise ValueError(f"cannot dispatch rework from status {defect['status']}")
            now = utcnow()
            self.db.execute(
                "INSERT INTO rework_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, defect_id, defect["lot_id"], assignee.strip(), instruction.strip(),
                 "open", actor.user_id, now, None, None, None),
            )
            old_status = defect["status"]
            self._update_status(defect, "rework_open", actor.user_id, reason, now)
            defect_event(self.db, defect_id, defect["lot_id"], "rework_dispatched", actor.user_id,
                         reason, {"task_id": task_id, "assignee": assignee, "instruction": instruction},
                         old_status, "rework_open")
            event(self.db, defect["lot_id"], "rework_dispatched", actor.user_id,
                  {"defect_id": defect_id, "task_id": task_id, "assignee": assignee})
            if self.db.execute("SELECT status FROM chip_lots WHERE lot_id=?",
                               (defect["lot_id"],)).fetchone()["status"] == "released":
                self._set_lot_hold(defect["lot_id"], actor.user_id, defect_id, "recalled")
        return self.get_defect(token, defect_id)

    def complete_rework(self, token: str, task_id: str, note: str, reason: str) -> dict:
        actor = self.auth.require(token, "rework")
        if not reason.strip():
            raise ValueError("reason is required")
        with transaction(self.db):
            task = self.db.execute("SELECT * FROM rework_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task:
                raise KeyError(task_id)
            if task["status"] != "open":
                raise ValueError(f"task {task_id} is {task['status']}")
            defect = self._locked(task["defect_id"])
            if defect["status"] != "rework_open":
                raise ValueError(f"defect is {defect['status']}, not under rework")
            now = utcnow()
            self.db.execute(
                "UPDATE rework_tasks SET status='completed',completed_by=?,completed_at=?,"
                "completion_note=? WHERE task_id=?",
                (actor.user_id, now, note.strip(), task_id),
            )
            self._update_status(defect, "reworked", actor.user_id, reason, now)
            defect_event(self.db, defect["defect_id"], defect["lot_id"], "rework_completed",
                         actor.user_id, reason, {"task_id": task_id, "note": note},
                         "rework_open", "reworked")
            event(self.db, defect["lot_id"], "rework_completed", actor.user_id,
                  {"defect_id": defect["defect_id"], "task_id": task_id})
        return self.get_defect(token, defect["defect_id"])

    # ---------------------------------------------------------------- 复测

    def record_retest(self, token: str, defect_id: str, result: str,
                      data: dict | None = None, note: str = "", reason: str = "") -> dict:
        actor = self.auth.require(token, "retest")
        if result not in ("pass", "fail") or not reason.strip():
            raise ValueError("result ('pass'/'fail') and reason are required")
        with transaction(self.db):
            defect = self._locked(defect_id)
            if defect["status"] != "reworked":
                raise ValueError(f"retest requires completed rework, defect is {defect['status']}")
            task = self.db.execute(
                "SELECT * FROM rework_tasks WHERE defect_id=? AND status='completed'"
                " ORDER BY completed_at DESC LIMIT 1", (defect_id,)).fetchone()
            retest_id = uuid.uuid4().hex
            now = utcnow()
            self.db.execute(
                "INSERT INTO retests VALUES(?,?,?,?,?,?,?,?,?)",
                (retest_id, defect_id, task["task_id"], defect["lot_id"], result,
                 json.dumps(data or {}, sort_keys=True),
                 actor.user_id, now, note.strip()),
            )
            new_status = "retest_passed" if result == "pass" else "retest_failed"
            self._update_status(defect, new_status, actor.user_id, reason, now)
            defect_event(self.db, defect_id, defect["lot_id"], f"retest_{result}", actor.user_id,
                         reason, {"retest_id": retest_id, "task_id": task["task_id"], "data": data or {}},
                         "reworked", new_status)
            event(self.db, defect["lot_id"], f"retest_{result}", actor.user_id,
                  {"defect_id": defect_id, "retest_id": retest_id})
            # 复测失败：已完成的返工任务退回 reopen，批次继续 hold；
            # 需重新派工产生新任务。
            if result == "fail":
                self.db.execute(
                    "UPDATE rework_tasks SET status='reopened' WHERE task_id=?",
                    (task["task_id"],))
        return self.get_defect(token, defect_id)

    # ---------------------------------------------------------------- 关闭

    def close_defect(self, token: str, defect_id: str, reason: str) -> dict:
        """质量人员闭环：严重缺陷必须先有通过的复测；minor 可凭让步接收关闭。"""
        actor = self.auth.require(token, "defect_close")
        if not reason.strip():
            raise ValueError("reason is required")
        with transaction(self.db):
            defect = self._locked(defect_id)
            if defect["status"] in ("closed", "void"):
                raise ValueError(f"defect already {defect['status']}")
            if defect["severity"] in BLOCKING_SEVERITIES and defect["status"] != "retest_passed":
                raise QualityGateError(
                    f"defect {defect_id} requires a passed retest before close"
                    f" (current: {defect['status']})"
                )
            now = utcnow()
            old_status = defect["status"]
            self._update_status(defect, "closed", actor.user_id, reason, now)
            defect_event(self.db, defect_id, defect["lot_id"], "closed", actor.user_id, reason,
                         {"severity": defect["severity"]}, old_status, "closed")
            event(self.db, defect["lot_id"], "defect_closed", actor.user_id,
                  {"defect_id": defect_id, "reason": reason})
        return self.get_defect(token, defect_id)

    def void_defect(self, token: str, defect_id: str, reason: str) -> dict:
        actor = self.auth.require(token, "defect_void")
        if not reason.strip():
            raise ValueError("reason is required")
        with transaction(self.db):
            defect = self._locked(defect_id)
            if defect["status"] == "void":
                raise ValueError("defect already void")
            if defect["status"] == "closed":
                raise ValueError("closed defect cannot be voided")
            now = utcnow()
            old_status = defect["status"]
            self._update_status(defect, "void", actor.user_id, reason, now)
            defect_event(self.db, defect_id, defect["lot_id"], "voided", actor.user_id, reason,
                         {}, old_status, "void")
            event(self.db, defect["lot_id"], "defect_voided", actor.user_id,
                  {"defect_id": defect_id, "reason": reason})
        return self.get_defect(token, defect_id)

    # ---------------------------------------------------------------- 内部

    def _locked(self, defect_id: str) -> dict:
        row = self.db.execute("SELECT * FROM defects WHERE defect_id=?", (defect_id,)).fetchone()
        if not row:
            raise KeyError(defect_id)
        return dict(row)

    def _update_status(self, defect: dict, new_status: str, actor: str, reason: str, now: str) -> None:
        if new_status not in DEFECT_STATUSES:
            raise ValueError(f"unknown status {new_status}")
        self.db.execute("UPDATE defects SET status=?,updated_at=? WHERE defect_id=?",
                        (new_status, now, defect["defect_id"]))
        defect["status"] = new_status

    def _set_lot_hold(self, lot_id: str, actor: str, defect_id: str, kind: str) -> None:
        self.db.execute("UPDATE chip_lots SET status='hold',updated_at=? WHERE lot_id=?",
                        (utcnow(), lot_id))
        event(self.db, lot_id, "defect_hold", actor,
              {"defect_id": defect_id, "kind": kind, "reason": "blocking defect registered"})
