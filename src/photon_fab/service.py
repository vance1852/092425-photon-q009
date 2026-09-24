"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from typing import Sequence

from .analytics import confidence_interval, summarize_spectrum, yield_rate
from .auth import Auth
from .storage import connect, event, transaction, utcnow


SEVERITIES = {"minor", "major", "critical"}
BLOCKING_SEVERITIES = {"major", "critical"}
DEFECT_STATUSES = {"open", "reworking", "retest_pending", "closed"}


class PhotonService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)
        # HTTP 服务在多线程中共享同一个 SQLite 连接，请求级串行化
        self.lock = threading.RLock()

    def bootstrap_admin(self, user_id: str = "admin", password: str = "photon-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, wafer_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if wafer_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)", (lot_id, product, process_rev, wafer_count, "engineering", actor.user_id, now, now))
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return dict(row)

    def add_measurement(self, token: str, lot_id: str, wavelength_nm: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(wavelength_nm), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "wavelength_nm": wavelength_nm})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def analyze(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "analyze")
        rows = self.db.execute("SELECT wavelength_nm,response FROM measurements WHERE lot_id=? ORDER BY wavelength_nm", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        summary = summarize_spectrum([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(self.get_lot(token, lot_id)["wafer_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {"lot_id": lot_id, "spectrum": summary.__dict__, "yield": rates, "response_ci": ci}

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValueError("decision and reason are required")
        with transaction(self.db):
            if decision == "release":
                self._guard_release(lot_id)
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)", (lot_id, actor.user_id, decision, reason, utcnow()))
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
        return self.get_lot(token, lot_id)

    def _guard_release(self, lot_id: str) -> None:
        open_defects = self.db.execute(
            "SELECT severity,COUNT(*) FROM defects WHERE lot_id=? AND status!='closed' GROUP BY severity",
            (lot_id,),
        ).fetchall()
        for row in open_defects:
            if row[0] in BLOCKING_SEVERITIES and row[1] > 0:
                raise PermissionError(f"release blocked: {row[1]} {row[0]} defect(s) not closed for lot {lot_id}")
        open_tasks = self.db.execute(
            "SELECT COUNT(*) FROM rework_tasks WHERE lot_id=? AND status!='done'", (lot_id,)
        ).fetchone()[0]
        if open_tasks:
            raise PermissionError(f"release blocked: {open_tasks} rework task(s) incomplete for lot {lot_id}")

    def register_defect(self, token: str, lot_id: str, category: str, title: str,
                        severity: str, description: str, owner: str, reason: str) -> dict:
        actor = self.auth.require(token, "rework")
        if severity not in SEVERITIES or not title.strip() or not category.strip() or not owner.strip() or not reason.strip():
            raise ValueError("severity, category, title, owner and reason are required")
        defect_id = uuid.uuid4().hex
        now = utcnow()
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute(
                "INSERT INTO defects VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (defect_id, lot_id, category, title, severity, description, owner,
                 "open", actor.user_id, now, now),
            )
            self._defect_event(lot_id, defect_id, "registered", None, "open", actor.user_id, reason)
            if severity in BLOCKING_SEVERITIES:
                self._hold_lot(lot_id, actor.user_id,
                               f"blocking {severity} defect {defect_id} registered: {reason}")
        return self.get_defect(token, defect_id)

    def list_defects(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM defects WHERE lot_id=? ORDER BY created_at", (lot_id,)).fetchall()]

    def get_defect(self, token: str, defect_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM defects WHERE defect_id=?", (defect_id,)).fetchone()
        if not row:
            raise KeyError(defect_id)
        result = dict(row)
        result["rework_tasks"] = [dict(r) for r in self.db.execute(
            "SELECT * FROM rework_tasks WHERE defect_id=? ORDER BY created_at", (defect_id,)).fetchall()]
        result["retests"] = [dict(r) for r in self.db.execute(
            "SELECT * FROM retests WHERE defect_id=? ORDER BY tested_at", (defect_id,)).fetchall()]
        return result

    def create_rework_task(self, token: str, defect_id: str, instruction: str,
                           assignee: str, reason: str) -> dict:
        actor = self.auth.require(token, "rework")
        if not instruction.strip() or not reason.strip():
            raise ValueError("instruction and reason are required")
        task_id = uuid.uuid4().hex
        now = utcnow()
        with transaction(self.db):
            defect = self._require_defect(defect_id)
            if defect["status"] not in {"open", "reworking"}:
                raise ValueError(f"defect {defect_id} is {defect['status']}, rework can only start on open defects")
            assignee_id = assignee.strip() or defect["owner"]
            self.db.execute(
                "INSERT INTO rework_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, defect_id, defect["lot_id"], instruction, assignee_id,
                 "open", actor.user_id, now, None, None, None),
            )
            if defect["status"] == "open":
                self._transition_defect(defect, "reworking", actor.user_id, reason, action="rework_created")
            else:
                self._defect_event(defect["lot_id"], defect_id, "rework_task_added",
                                   defect["status"], defect["status"], actor.user_id, reason)
        return {"task_id": task_id, "defect_id": defect_id, "assignee": assignee_id, "status": "open"}

    def complete_rework_task(self, token: str, task_id: str, note: str) -> dict:
        actor = self.auth.require(token, "rework")
        if not note.strip():
            raise ValueError("completion note (reason) is required")
        with transaction(self.db):
            task = self.db.execute("SELECT * FROM rework_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task:
                raise KeyError(task_id)
            if task["status"] == "done":
                raise ValueError(f"rework task {task_id} is already done")
            now = utcnow()
            self.db.execute(
                "UPDATE rework_tasks SET status='done',completed_by=?,completed_at=?,completion_note=? WHERE task_id=?",
                (actor.user_id, now, note, task_id),
            )
            defect = self._require_defect(task["defect_id"])
            remaining = self.db.execute(
                "SELECT COUNT(*) FROM rework_tasks WHERE defect_id=? AND status!='done'", (defect["defect_id"],)
            ).fetchone()[0]
            if remaining == 0:
                self._transition_defect(defect, "retest_pending", actor.user_id, note, action="rework_completed")
            else:
                self._defect_event(defect["lot_id"], defect["defect_id"], "rework_completed",
                                   defect["status"], defect["status"], actor.user_id,
                                   f"{note} (rework task {task_id} done, {remaining} task(s) remain)")
        return {"task_id": task_id, "status": "done", "completed_by": actor.user_id, "completed_at": now}

    def record_retest(self, token: str, defect_id: str, result: str, reason: str,
                      measurement_id: str | None = None) -> dict:
        actor = self.auth.require(token, "rework")
        if result not in {"pass", "fail"} or not reason.strip():
            raise ValueError("retest result (pass/fail) and reason are required")
        retest_id = uuid.uuid4().hex
        now = utcnow()
        with transaction(self.db):
            defect = self._require_defect(defect_id)
            if defect["status"] != "retest_pending":
                raise ValueError(
                    f"defect {defect_id} is {defect['status']}; retest requires completed rework (retest_pending)")
            if measurement_id and not self.db.execute(
                "SELECT 1 FROM measurements WHERE measurement_id=? AND lot_id=?",
                (measurement_id, defect["lot_id"]),
            ).fetchone():
                raise ValueError(f"measurement {measurement_id} does not belong to lot {defect['lot_id']}")
            self.db.execute(
                "INSERT INTO retests VALUES(?,?,?,?,?,?,?,?)",
                (retest_id, defect_id, defect["lot_id"], result, reason,
                 measurement_id, actor.user_id, now),
            )
            if result == "pass":
                self._transition_defect(defect, "closed", actor.user_id, reason, action="retest_passed")
            else:
                self._transition_defect(defect, "open", actor.user_id, reason, action="retest_failed")
        return {"retest_id": retest_id, "defect_id": defect_id, "result": result, "tested_by": actor.user_id}

    def close_minor_defect(self, token: str, defect_id: str, reason: str) -> dict:
        """次要缺陷允许质量人员不经过复测直接关闭；major/critical 必须复测通过。"""
        actor = self.auth.require(token, "approve")
        if not reason.strip():
            raise ValueError("reason is required")
        with transaction(self.db):
            defect = self._require_defect(defect_id)
            if defect["severity"] in BLOCKING_SEVERITIES:
                raise PermissionError(f"{defect['severity']} defect {defect_id} can only be closed by a passing retest")
            self._transition_defect(defect, "closed", actor.user_id, reason, action="closed_without_retest")
        return self.get_defect(token, defect_id)

    def defect_history(self, token: str, defect_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM defect_events WHERE defect_id=? ORDER BY event_id", (defect_id,)).fetchall()]

    def _require_defect(self, defect_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM defects WHERE defect_id=?", (defect_id,)).fetchone()
        if not row:
            raise KeyError(defect_id)
        return row

    def _transition_defect(self, defect: sqlite3.Row, to_status: str, actor: str,
                           reason: str, action: str) -> None:
        from_status = defect["status"]
        self.db.execute(
            "UPDATE defects SET status=?,updated_at=? WHERE defect_id=?",
            (to_status, utcnow(), defect["defect_id"]),
        )
        self._defect_event(defect["lot_id"], defect["defect_id"], action,
                           from_status, to_status, actor, reason)

    def _defect_event(self, lot_id: str, defect_id: str, action: str, from_status: str | None,
                      to_status: str, actor: str, reason: str) -> None:
        self.db.execute(
            "INSERT INTO defect_events(lot_id,defect_id,action,from_status,to_status,actor,reason,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (lot_id, defect_id, action, from_status, to_status, actor, reason, utcnow()),
        )

    def _hold_lot(self, lot_id: str, actor: str, reason: str) -> None:
        self.db.execute("UPDATE chip_lots SET status='hold',updated_at=? WHERE lot_id=? AND status!='rejected'",
                        (utcnow(), lot_id))
        event(self.db, lot_id, "quality_hold", actor, {"reason": reason})

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
