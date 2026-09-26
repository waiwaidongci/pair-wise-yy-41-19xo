from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, parse_utc_time
from .rules import ID_PREFIX, STATES, calibration_active, windows_overlap


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS calibrations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    point_code TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    calibrated_at TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_until TEXT NOT NULL,
                    correction_factor REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active'
                        CHECK(state IN ('active','superseded')),
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_calibrations_point
                    ON calibrations(point_code, state);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)
            self._migrate_columns("items", {
                "point_code": "TEXT",
                "device_id": "TEXT",
                "raw_quantity": "REAL",
                "calibration_id": "INTEGER",
                "correction_factor": "REAL",
            })

    def _migrate_columns(self, table: str, columns: Dict[str, str]) -> None:
        existing = {row["name"] for row in self.conn.execute(
            f"PRAGMA table_info({table})").fetchall()}
        for name, decl in columns.items():
            if name not in existing:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, point_code: Optional[str] = None,
                    device_id: Optional[str] = None, raw_quantity: Optional[float] = None,
                    calibration_id: Optional[int] = None,
                    correction_factor: Optional[float] = None) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       point_code, device_id, raw_quantity, calibration_id, correction_factor)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now, point_code, device_id, raw_quantity,
                     calibration_id, correction_factor),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---- 校准台账 ----
    @staticmethod
    def _calibration(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def list_calibrations(self, point_code: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM calibrations"
        params: tuple = ()
        if point_code is not None:
            sql += " WHERE point_code=?"
            params = (point_code,)
        sql += " ORDER BY point_code, id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._calibration(row) for row in rows]

    def get_calibration(self, calibration_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM calibrations WHERE id=?", (calibration_id,)).fetchone()
        if row is None:
            raise NotFoundError("校准记录不存在")
        return self._calibration(row)

    def list_active_calibrations(self, point_code: str, now) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM calibrations WHERE point_code=? AND state='active' ORDER BY id",
                (point_code,),
            ).fetchall()
        return [self._calibration(row) for row in rows
                if calibration_active(self._calibration(row), now)]

    def list_all_active_by_point(self, now) -> Dict[str, List[Dict[str, Any]]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM calibrations WHERE state='active' ORDER BY id").fetchall()
        result: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            cal = self._calibration(row)
            if calibration_active(cal, now):
                result.setdefault(cal["point_code"], []).append(cal)
        return result

    def register_calibration(self, point_code: str, device_id: str,
                             calibrated_at: str, valid_from: str, valid_until: str,
                             correction_factor: float, note: str, actor: str,
                             replace: bool, now) -> Dict[str, Any]:
        start = parse_utc_time(valid_from)
        end = parse_utc_time(valid_until)
        with self._lock, self.conn:
            overlaps = []
            for row in self.conn.execute(
                    "SELECT * FROM calibrations WHERE point_code=? AND state='active' ORDER BY id",
                    (point_code,)).fetchall():
                existing = self._calibration(row)
                ex_start = parse_utc_time(existing["valid_from"])
                ex_end = parse_utc_time(existing["valid_until"])
                if ex_start and ex_end and windows_overlap(start, end, ex_start, ex_end):
                    overlaps.append(existing)
            if overlaps and not replace:
                ids = ",".join(str(c["id"]) for c in overlaps)
                raise ConflictError(f"同一测点存在生效区间重叠的校准记录（编号{ids}），如需替换请声明replace=true")
            superseded_ids: List[int] = []
            if overlaps:
                for cal in overlaps:
                    self.conn.execute(
                        "UPDATE calibrations SET state='superseded' WHERE id=?", (cal["id"],))
                    superseded_ids.append(cal["id"])
            cur = self.conn.execute(
                """INSERT INTO calibrations(point_code, device_id, calibrated_at, valid_from,
                   valid_until, correction_factor, state, note, created_by, created_at)
                   VALUES(?,?,?,?,?,?, 'active', ?,?,?)""",
                (point_code, device_id, calibrated_at, valid_from, valid_until,
                 correction_factor, note, actor, utc_now()),
            )
            calibration_id = int(cur.lastrowid)
        result = self.get_calibration(calibration_id)
        result["superseded_ids"] = superseded_ids
        return result

    def list_open_items_by_point(self, point_code: str) -> List[Dict[str, Any]]:
        from .rules import OPEN_STATES
        placeholders = ",".join("?" for _ in OPEN_STATES)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM items WHERE point_code=? AND status IN ({placeholders}) ORDER BY id",
                (point_code, *sorted(OPEN_STATES)),
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_item_calibration(self, item_id: int, quantity: float,
                               calibration_id: int, correction_factor: float,
                               actor: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE items SET quantity=?, calibration_id=?, correction_factor=?,
                   device_id=(SELECT device_id FROM calibrations WHERE id=?),
                   version=version+1, updated_at=? WHERE id=?""",
                (quantity, calibration_id, correction_factor, calibration_id,
                 utc_now(), item_id),
            )

    def close(self) -> None:
        with self._lock:
            self.conn.close()
