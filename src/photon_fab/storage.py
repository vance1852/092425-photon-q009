"""芯片批次和测量记录的 SQLite 结构及事务辅助函数。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 wafer_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 wavelength_nm REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
CREATE TABLE IF NOT EXISTS defects(
 defect_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 code TEXT NOT NULL, title TEXT NOT NULL, severity TEXT NOT NULL,
 source TEXT NOT NULL, owner TEXT NOT NULL, status TEXT NOT NULL,
 created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rework_tasks(
 task_id TEXT PRIMARY KEY, defect_id TEXT NOT NULL REFERENCES defects(defect_id),
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id), assignee TEXT NOT NULL,
 instruction TEXT NOT NULL, status TEXT NOT NULL,
 created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 completed_by TEXT, completed_at TEXT, completion_note TEXT);
CREATE TABLE IF NOT EXISTS retests(
 retest_id TEXT PRIMARY KEY, defect_id TEXT NOT NULL REFERENCES defects(defect_id),
 task_id TEXT NOT NULL REFERENCES rework_tasks(task_id),
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id), result TEXT NOT NULL,
 data TEXT, tested_by TEXT NOT NULL, tested_at TEXT NOT NULL, note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS defect_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, defect_id TEXT NOT NULL,
 lot_id TEXT NOT NULL, event_type TEXT NOT NULL, from_status TEXT, to_status TEXT,
 actor TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
"""

# 严重度：minor 不阻塞放行但必须闭环；major/critical 为严重缺陷，
# 未完成返工并复测通过前批次保持 hold。
SEVERITIES = ("minor", "major", "critical")
BLOCKING_SEVERITIES = ("major", "critical")
DEFECT_STATUSES = (
    "open", "rework_open", "reworked", "retest_passed", "retest_failed", "closed", "void",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # HTTP 服务使用 ThreadingHTTPServer；写事务均以 BEGIN IMMEDIATE 串行化。
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    db.commit()
    return db


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def event(db: sqlite3.Connection, lot_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute("INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)", (lot_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()))


def defect_event(db: sqlite3.Connection, defect_id: str, lot_id: str, event_type: str,
                 actor: str, reason: str, payload: dict | None = None,
                 from_status: str | None = None, to_status: str | None = None) -> None:
    db.execute(
        "INSERT INTO defect_events(defect_id,lot_id,event_type,from_status,to_status,actor,reason,payload,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (defect_id, lot_id, event_type, from_status, to_status, actor, reason,
         json.dumps(payload or {}, sort_keys=True), utcnow()),
    )
