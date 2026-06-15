"""SQLite event queue + audit log + pending approvals + branch activity.

A single database file (default ``~/.gitpilot/gitpilot.db``) backs the daemon.
No external database is used in v1. All writes that record an executed action
must set ``approved_by='human'`` — there is no 'auto'/'agent' path.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    approved_by TEXT,              -- 'human' always — never 'auto' or 'agent'
    recommendation TEXT,
    outcome TEXT,
    risk_level TEXT,
    confidence INTEGER,           -- always 0-95, never 100
    reason TEXT,
    llm_provider TEXT,
    dry_run BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pending_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    target TEXT NOT NULL,
    analysis TEXT NOT NULL,        -- full JSON analysis for display on approve
    risk_level TEXT,
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS branch_activity (
    branch_name TEXT PRIMARY KEY,
    last_push TIMESTAMP,
    classification TEXT,
    classification_confidence INTEGER,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

DEFAULT_DB_PATH = Path.home() / ".gitpilot" / "gitpilot.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def enqueue_event(self, event_type: str, payload: dict | str) -> int:
        payload_json = payload if isinstance(payload, str) else json.dumps(payload)
        cur = self.conn.execute(
            "INSERT INTO events (event_type, payload) VALUES (?, ?)",
            (event_type, payload_json),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_next_pending_event(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM events WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def mark_processing(self, event_id: int) -> None:
        self.conn.execute("UPDATE events SET status='processing' WHERE id=?", (event_id,))
        self.conn.commit()

    def mark_done(self, event_id: int) -> None:
        self.conn.execute("UPDATE events SET status='done' WHERE id=?", (event_id,))
        self.conn.commit()

    def mark_failed(self, event_id: int) -> None:
        self.conn.execute("UPDATE events SET status='failed' WHERE id=?", (event_id,))
        self.conn.commit()

    # ------------------------------------------------------------------
    # audit_log
    # ------------------------------------------------------------------
    def log_audit(
        self,
        action: str,
        target: str,
        status: str,
        *,
        approved_by: str | None = None,
        recommendation: str | None = None,
        outcome: str | None = None,
        risk_level: str | None = None,
        confidence: int | None = None,
        reason: str | None = None,
        llm_provider: str | None = None,
        dry_run: bool = False,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO audit_log
                (action, target, status, approved_by, recommendation, outcome,
                 risk_level, confidence, reason, llm_provider, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action, target, status, approved_by, recommendation, outcome,
                risk_level, confidence, reason, llm_provider, 1 if dry_run else 0,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def recent_audit(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def audit_counts_today(self) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT status, COUNT(*) AS n FROM audit_log
            WHERE date(created_at) = date('now')
            GROUP BY status
            """
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def last_audit(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # pending_approvals
    # ------------------------------------------------------------------
    def add_pending_approval(
        self,
        action_type: str,
        target: str,
        analysis: dict | str,
        risk_level: str = "low",
        ttl_seconds: int | None = None,
    ) -> int:
        analysis_json = analysis if isinstance(analysis, str) else json.dumps(analysis)
        expires_at = None
        if ttl_seconds:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        cur = self.conn.execute(
            """
            INSERT INTO pending_approvals (action_type, target, analysis, risk_level, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (action_type, target, analysis_json, risk_level, expires_at),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_pending_approval(self, approval_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM pending_approvals WHERE id=?", (approval_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_pending_approvals(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM pending_approvals ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_pending_approval(self, approval_id: int) -> None:
        self.conn.execute("DELETE FROM pending_approvals WHERE id=?", (approval_id,))
        self.conn.commit()

    # ------------------------------------------------------------------
    # branch_activity
    # ------------------------------------------------------------------
    def update_branch_last_push(self, branch_name: str, timestamp: str) -> None:
        self.conn.execute(
            """
            INSERT INTO branch_activity (branch_name, last_push, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(branch_name) DO UPDATE SET last_push=excluded.last_push, updated_at=excluded.updated_at
            """,
            (branch_name, timestamp, _now()),
        )
        self.conn.commit()

    def update_branch_classification(
        self, branch_name: str, classification: str, confidence: int
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO branch_activity (branch_name, classification, classification_confidence, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(branch_name) DO UPDATE SET
                classification=excluded.classification,
                classification_confidence=excluded.classification_confidence,
                updated_at=excluded.updated_at
            """,
            (branch_name, classification, confidence, _now()),
        )
        self.conn.commit()

    def get_branch_activity(self, branch_name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM branch_activity WHERE branch_name=?", (branch_name,)
        ).fetchone()
        return dict(row) if row else None
