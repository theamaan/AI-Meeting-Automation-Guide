"""
database.py — SQLite Storage Layer
Persists every meeting: file path, raw transcript, MOM JSON, notification status.
Using context manager pattern for connection safety.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT    UNIQUE NOT NULL,
    file_name       TEXT    NOT NULL,
    file_type       TEXT    NOT NULL,
    processed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    transcript      TEXT,
    mom_json        TEXT,
    meeting_date    TEXT,
    meeting_title   TEXT,
    status          TEXT    DEFAULT 'pending',
    error_message   TEXT,
    teams_notified  INTEGER DEFAULT 0,
    email_sent      INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_meetings_file_path    ON meetings(file_path);
CREATE INDEX IF NOT EXISTS idx_meetings_status       ON meetings(status);
CREATE INDEX IF NOT EXISTS idx_meetings_processed_at ON meetings(processed_at);
"""


# ──────────────────────────────────────────────────────────────
# Database class
# ──────────────────────────────────────────────────────────────

class Database:
    def __init__(self, db_path: str = "data/meetings.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.info(f"Database ready: {db_path}")

    # ── Internal ──────────────────────────────────────────────

    @contextmanager
    def _conn(self):
        """Thread-safe connection with auto commit/rollback."""
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent reads
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ── Write operations ──────────────────────────────────────

    def record_meeting(self, file_path: str, file_type: str) -> int:
        """
        Register a new file for processing.
        INSERT OR IGNORE — safe to call multiple times; won't duplicate.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO meetings (file_path, file_name, file_type, status)
                VALUES (?, ?, ?, 'processing')
                """,
                (file_path, Path(file_path).name, file_type),
            )
            return cursor.lastrowid

    def update_transcript(self, file_path: str, transcript: str):
        """Save raw transcript text after parsing."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE meetings SET transcript = ? WHERE file_path = ?",
                (transcript, file_path),
            )

    def update_mom(
        self,
        file_path: str,
        mom_data: Dict,
        meeting_title: str = "",
        meeting_date: str = "",
    ):
        """Save generated MOM JSON and mark as completed."""
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE meetings
                SET mom_json = ?, meeting_title = ?, meeting_date = ?, status = 'completed'
                WHERE file_path = ?
                """,
                (json.dumps(mom_data, ensure_ascii=False), meeting_title, meeting_date, file_path),
            )

    def mark_notification_sent(self, file_path: str, teams: bool = False, email: bool = False):
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE meetings
                SET teams_notified = ?, email_sent = ?
                WHERE file_path = ?
                """,
                (1 if teams else 0, 1 if email else 0, file_path),
            )

    def mark_failed(self, file_path: str, error: str):
        """Mark a file as failed with truncated error message."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE meetings SET status = 'failed', error_message = ? WHERE file_path = ?",
                (error[:1000], file_path),
            )

    # ── Read operations ───────────────────────────────────────

    def is_processed(self, file_path: str) -> bool:
        """Return True only if this exact file was successfully completed before."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM meetings WHERE file_path = ? AND status = 'completed'",
                (file_path,),
            ).fetchone()
            return row is not None

    def get_mom(self, file_path: str) -> Optional[Dict]:
        """Retrieve previously generated MOM JSON for a file."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT mom_json FROM meetings WHERE file_path = ?",
                (file_path,),
            ).fetchone()
            if row and row["mom_json"]:
                return json.loads(row["mom_json"])
        return None

    def get_recent_meetings(self, limit: int = 20) -> List[Dict]:
        """Return recent meetings for status dashboard."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, file_name, meeting_title, meeting_date, status,
                       processed_at, teams_notified, email_sent, error_message
                FROM meetings
                ORDER BY processed_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_failed_meetings(self) -> List[Dict]:
        """Return all failed meetings for retry."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT file_path, file_name, error_message FROM meetings WHERE status = 'failed'"
            ).fetchall()
            return [dict(row) for row in rows]
