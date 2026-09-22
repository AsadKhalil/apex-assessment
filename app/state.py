"""SQLite state: identity, conversations, messages, pending actions, ledger, audit, mock hospital."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from app.config import Settings, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
  patient_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, token TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS slots (
  slot_id TEXT PRIMARY KEY, department TEXT NOT NULL, clinician TEXT NOT NULL,
  start_time TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open');
CREATE TABLE IF NOT EXISTS appointments (
  appointment_id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, slot_id TEXT NOT NULL,
  department TEXT NOT NULL, clinician TEXT NOT NULL, start_time TEXT NOT NULL,
  status TEXT NOT NULL, location TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS escalations (
  escalation_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, patient_id TEXT NOT NULL,
  reason_category TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (
  conversation_id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, state TEXT NOT NULL,
  turn_index INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_actions (
  token TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, tool TEXT NOT NULL, args_json TEXT NOT NULL,
  args_hash TEXT NOT NULL, summary TEXT NOT NULL, turn_created INTEGER NOT NULL,
  expires_at TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS action_ledger (
  idempotency_key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, tool TEXT NOT NULL,
  args_hash TEXT NOT NULL, result_json TEXT NOT NULL, verified INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, request_id TEXT NOT NULL,
  conversation_id TEXT, patient_hash TEXT, event TEXT NOT NULL, detail_json TEXT NOT NULL);
"""

# department -> (clinician, location)
DEPARTMENTS = {
    "cardiology": ("Dr. A. Rahman", "Main Hospital, Building A, Floor 2"),
    "dermatology": ("Dr. L. Haddad", "Main Hospital, Building B, Floor 1"),
    "dental": ("Dr. S. Noor", "Dental Center, Ground Floor"),
    "radiology": ("Dr. M. Qureshi", "Main Hospital, Building A, Floor 0"),
}
SLOT_TIMES = ((9, 0), (11, 0), (14, 0))


def workdays(start: datetime, count: int = 14) -> list[datetime]:
    """The next `count` calendar days after `start`, keeping Sunday..Thursday (Saudi work week)."""
    days = [start + timedelta(days=i) for i in range(1, count + 1)]
    return [d for d in days if d.weekday() not in (4, 5)]  # Friday=4, Saturday=5


def slot_id_for(department: str, when: datetime) -> str:
    return f"S-{department}-{when:%Y%m%d-%H%M}"


def _rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        if settings.db_path != ":memory:":
            Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(settings.db_path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")

    def now(self) -> datetime:
        return now(self.settings)

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)

    def seed(self) -> None:
        if self.conn.execute("SELECT 1 FROM patients LIMIT 1").fetchone():
            return
        t = self.now()
        days = workdays(t)
        with self.transaction():
            self.conn.executemany(
                "INSERT INTO patients VALUES (?,?,?)",
                [("P-1001", "Patient One", "token-p1001"), ("P-1002", "Patient Two", "token-p1002")],
            )
            for dept, (clinician, _) in DEPARTMENTS.items():
                for day in days:
                    for hh, mm in SLOT_TIMES:
                        when = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
                        self.conn.execute(
                            "INSERT INTO slots VALUES (?,?,?,?,'open')",
                            (slot_id_for(dept, when), dept, clinician, when.isoformat()),
                        )
            seeded = [
                ("A-1001-1", "P-1001", "cardiology", days[2], 9),
                ("A-1001-2", "P-1001", "dermatology", days[1], 14),
                ("A-1002-1", "P-1002", "dental", days[1], 11),
                ("A-1002-2", "P-1002", "radiology", days[3], 9),
            ]
            for appt_id, pid, dept, day, hour in seeded:
                when = day.replace(hour=hour, minute=0, second=0, microsecond=0)
                sid = slot_id_for(dept, when)
                clinician, location = DEPARTMENTS[dept]
                self.conn.execute("UPDATE slots SET status='taken' WHERE slot_id=?", (sid,))
                self.conn.execute(
                    "INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?,?)",
                    (appt_id, pid, sid, dept, clinician, when.isoformat(), "booked", location, t.isoformat()),
                )

    # ---- identity ----
    def patient_for_token(self, token: str) -> str | None:
        row = self.conn.execute("SELECT patient_id FROM patients WHERE token=?", (token,)).fetchone()
        return row["patient_id"] if row else None

    # ---- conversations ----
    def create_conversation(self, patient_id: str) -> str:
        cid = str(uuid.uuid4())
        ts = self.now().isoformat()
        self.conn.execute(
            "INSERT INTO conversations VALUES (?,?,?,?,?,?)", (cid, patient_id, "IDLE", 0, ts, ts)
        )
        return cid

    def get_conversation(self, cid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT conversation_id, patient_id, state, turn_index FROM conversations WHERE conversation_id=?",
            (cid,),
        ).fetchone()
        return dict(row) if row else None

    def set_state(self, cid: str, state: str) -> None:
        self.conn.execute(
            "UPDATE conversations SET state=?, updated_at=? WHERE conversation_id=?",
            (state, self.now().isoformat(), cid),
        )

    def next_turn(self, cid: str) -> int:
        self.conn.execute("UPDATE conversations SET turn_index=turn_index+1 WHERE conversation_id=?", (cid,))
        return self.get_conversation(cid)["turn_index"]

    # ---- messages ----
    def append_message(self, cid: str, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?,?,?,?)",
            (cid, role, content, self.now().isoformat()),
        )

    def list_messages(self, cid: str, limit: int = 20) -> list[dict]:
        rows = _rows(self.conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?", (cid, limit)
        ))
        return list(reversed(rows))

    # ---- pending actions (exactly one open per conversation) ----
    def set_pending(self, cid: str, *, token: str, tool: str, args: dict, args_hash: str, summary: str,
                    turn_created: int, expires_at: str) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE pending_actions SET status='replaced' WHERE conversation_id=? AND status='pending'", (cid,)
            )
            self.conn.execute(
                "INSERT INTO pending_actions VALUES (?,?,?,?,?,?,?,?,'pending')",
                (token, cid, tool, json.dumps(args, sort_keys=True), args_hash, summary, turn_created, expires_at),
            )

    def get_pending(self, cid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM pending_actions WHERE conversation_id=? AND status='pending'", (cid,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["args"] = json.loads(d.pop("args_json"))
        return d

    def close_pending(self, cid: str, status: str) -> None:
        self.conn.execute(
            "UPDATE pending_actions SET status=? WHERE conversation_id=? AND status='pending'", (status, cid)
        )

    # ---- ledger ----
    def ledger_get(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM action_ledger WHERE idempotency_key=?", (key,)).fetchone()
        if not row:
            return None
        return {"tool": row["tool"], "args_hash": row["args_hash"],
                "result": json.loads(row["result_json"]), "verified": bool(row["verified"])}

    def ledger_put(self, key: str, cid: str, tool: str, args_hash: str, result: dict, verified: bool) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO action_ledger VALUES (?,?,?,?,?,?,?)",
            (key, cid, tool, args_hash, json.dumps(result, sort_keys=True), int(verified), self.now().isoformat()),
        )

    # ---- audit ----
    def audit(self, *, request_id: str, conversation_id: str | None, patient_hash: str | None,
              event: str, **detail) -> None:
        self.conn.execute(
            "INSERT INTO audit_events (ts, request_id, conversation_id, patient_hash, event, detail_json)"
            " VALUES (?,?,?,?,?,?)",
            (self.now().isoformat(), request_id, conversation_id, patient_hash, event,
             json.dumps(detail, sort_keys=True, default=str)),
        )

    def list_audit(self, cid: str) -> list[dict]:
        rows = _rows(self.conn.execute(
            "SELECT request_id, event, detail_json FROM audit_events WHERE conversation_id=? ORDER BY id", (cid,)
        ))
        return [{"request_id": r["request_id"], "event": r["event"], "detail": json.loads(r["detail_json"])}
                for r in rows]

    # ---- hospital lookups ----
    def appointment(self, appointment_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM appointments WHERE appointment_id=?", (appointment_id,)).fetchone()
        return dict(row) if row else None

    def slot(self, slot_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
        return dict(row) if row else None
