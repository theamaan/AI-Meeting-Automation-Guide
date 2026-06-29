"""
database.py — SQL Server Storage Layer
Persists every meeting: file path, raw transcript, MOM JSON, notification status.
Migrated from SQLite to Microsoft SQL Server via pyodbc.

Connection string is built by DatabaseConfig.build_connection_string() in config.py.
Supports both Windows Authentication (default) and SQL Authentication.
"""

import json
import logging
import pyodbc
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Enable ODBC driver-level connection pooling (default is True; stated explicitly for clarity)
pyodbc.pooling = True


# ──────────────────────────────────────────────────────────────
# Schema DDL (T-SQL)
# ──────────────────────────────────────────────────────────────

_CREATE_TABLE = """
IF NOT EXISTS (
    SELECT 1 FROM sys.tables
    WHERE name = N'meetings' AND schema_id = SCHEMA_ID(N'dbo')
)
BEGIN
    CREATE TABLE dbo.meetings (
        id                  INT             IDENTITY(1,1)        NOT NULL,
        file_path           NVARCHAR(450)                        NOT NULL,
        file_name           NVARCHAR(260)                        NOT NULL,
        file_type           NVARCHAR(20)                         NOT NULL,
        file_hash           NVARCHAR(64)                         NULL,
        processed_at        DATETIME2(0)    DEFAULT GETUTCDATE() NOT NULL,
        transcript          NVARCHAR(MAX)                        NULL,
        mom_json            NVARCHAR(MAX)                        NULL,
        attendance_json     NVARCHAR(MAX)                        NULL,
        sentiment_json      NVARCHAR(MAX)                        NULL,
        meeting_date        DATE                                 NULL,
        meeting_title       NVARCHAR(500)                        NULL,
        status              NVARCHAR(30)    DEFAULT N'pending'   NOT NULL,
        error_message       NVARCHAR(2000)                       NULL,
        teams_notified      BIT             DEFAULT 0            NOT NULL,
        email_sent          BIT             DEFAULT 0            NOT NULL,
        approval_token      NVARCHAR(64)                         NULL,
        approval_status     NVARCHAR(30)    DEFAULT N'not_required' NULL,
        CONSTRAINT PK_meetings
            PRIMARY KEY CLUSTERED (id ASC) WITH (FILLFACTOR = 90),
        CONSTRAINT UQ_meetings_file_path
            UNIQUE NONCLUSTERED (file_path),
        CONSTRAINT CK_meetings_status CHECK (
            status IN (
                N'pending', N'processing', N'completed',
                N'failed', N'awaiting_approval', N'rejected'
            )
        ),
        CONSTRAINT CK_meetings_approval_status CHECK (
            approval_status IS NULL
            OR approval_status IN (
                N'not_required', N'pending', N'approved', N'rejected',
                N'auto_approved', N'auto_rejected'
            )
        )
    )
END
"""

_CREATE_INDEXES = [
    # Status-based lookups — most frequent query from watcher
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = N'IX_meetings_status'
          AND object_id = OBJECT_ID(N'dbo.meetings')
    )
    CREATE NONCLUSTERED INDEX IX_meetings_status
        ON dbo.meetings (status)
        INCLUDE (file_path, file_name, processed_at)
        WITH (FILLFACTOR = 90)
    """,
    # Recent-meetings dashboard — ORDER BY processed_at DESC
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = N'IX_meetings_processed_at'
          AND object_id = OBJECT_ID(N'dbo.meetings')
    )
    CREATE NONCLUSTERED INDEX IX_meetings_processed_at
        ON dbo.meetings (processed_at DESC)
        INCLUDE (file_name, meeting_title, status, teams_notified, email_sent, error_message)
        WITH (FILLFACTOR = 80)
    """,
    # Date-range query for weekly digest (Feature 10) — filtered index on completed only
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = N'IX_meetings_meeting_date_status'
          AND object_id = OBJECT_ID(N'dbo.meetings')
    )
    CREATE NONCLUSTERED INDEX IX_meetings_meeting_date_status
        ON dbo.meetings (meeting_date ASC)
        INCLUDE (file_name, meeting_title, mom_json, attendance_json)
        WHERE status = N'completed'
        WITH (FILLFACTOR = 90)
    """,
    # Approval token lookup — sparse filtered index (only non-NULL tokens)
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = N'IX_meetings_approval_token'
          AND object_id = OBJECT_ID(N'dbo.meetings')
    )
    CREATE NONCLUSTERED INDEX IX_meetings_approval_token
        ON dbo.meetings (approval_token)
        WHERE approval_token IS NOT NULL
        WITH (FILLFACTOR = 100)
    """,
]


# ──────────────────────────────────────────────────────────────
# Database class
# ──────────────────────────────────────────────────────────────

class Database:
    def __init__(self, connection_string: str, connection_timeout: int = 30):
        self._connection_string = connection_string
        self._timeout = connection_timeout
        self._init_db()
        logger.info("Database ready (SQL Server)")

    # ── Internal helpers ──────────────────────────────────────

    @contextmanager
    def _conn(self):
        """Thread-safe connection with automatic commit/rollback."""
        conn = pyodbc.connect(self._connection_string, timeout=self._timeout)
        conn.autocommit = False
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _to_dict(cursor, row) -> dict:
        """Convert a pyodbc Row to a plain dict using cursor.description."""
        return {desc[0]: val for desc, val in zip(cursor.description, row)}

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(_CREATE_TABLE)
            for idx_sql in _CREATE_INDEXES:
                conn.execute(idx_sql)
        self._migrate_db()

    def _migrate_db(self):
        """
        Idempotent column additions — safe to run on existing databases.
        Checks sys.columns before each ALTER TABLE so re-runs are harmless.
        Column names are internal constants, not user input — interpolation is safe.
        """
        migrations = [
            ("attendance_json",  "NVARCHAR(MAX) NULL"),
            ("sentiment_json",   "NVARCHAR(MAX) NULL"),
            ("approval_token",   "NVARCHAR(64)  NULL"),
            ("approval_status",  "NVARCHAR(30)  NULL"),
        ]
        for col_name, col_def in migrations:
            try:
                with self._conn() as conn:
                    conn.execute(f"""
                        IF NOT EXISTS (
                            SELECT 1 FROM sys.columns
                            WHERE object_id = OBJECT_ID(N'dbo.meetings')
                              AND name = N'{col_name}'
                        )
                        BEGIN
                            ALTER TABLE dbo.meetings ADD {col_name} {col_def}
                        END
                    """)
            except Exception as exc:
                logger.debug("Migration note for column %s: %s", col_name, exc)

    # ── Write operations ──────────────────────────────────────

    def record_meeting(self, file_path: str, file_type: str, file_hash: str = None) -> Optional[int]:
        """
        Register a file for processing.
        - Same path + same hash already recorded → return existing id (no-op).
        - Same path + different hash (new meeting at same filename) → delete old, insert fresh.
        - New path → insert and return new id.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT id, file_hash FROM dbo.meetings WHERE file_path = ?",
                (file_path,),
            )
            existing = cursor.fetchone()

            if existing:
                existing_id, existing_hash = existing[0], existing[1]
                if file_hash is not None and existing_hash != file_hash:
                    # Different content at same path — new meeting. Remove stale record.
                    conn.execute("DELETE FROM dbo.meetings WHERE file_path = ?", (file_path,))
                else:
                    return existing_id

            # Insert fresh record; OUTPUT INSERTED.id returns the new identity value
            cursor = conn.execute(
                """
                INSERT INTO dbo.meetings (file_path, file_name, file_type, file_hash, status)
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, ?, N'processing')
                """,
                (file_path, Path(file_path).name, file_type, file_hash),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def update_transcript(self, file_path: str, transcript: str):
        """Save raw transcript text after parsing."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE dbo.meetings SET transcript = ? WHERE file_path = ?",
                (transcript, file_path),
            )

    def update_attendance(self, file_path: str, attendance: Dict):
        """Store classified attendance result (spoke / silent / absent / unknown)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE dbo.meetings SET attendance_json = ? WHERE file_path = ?",
                (json.dumps(attendance, ensure_ascii=False), file_path),
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
                UPDATE dbo.meetings
                SET mom_json = ?, meeting_title = ?, meeting_date = ?, status = N'completed'
                WHERE file_path = ?
                """,
                (
                    json.dumps(mom_data, ensure_ascii=False),
                    meeting_title,
                    meeting_date or None,   # empty string → NULL for DATE column
                    file_path,
                ),
            )

    def mark_notification_sent(self, file_path: str, teams: bool = False, email: bool = False):
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE dbo.meetings
                SET teams_notified = ?, email_sent = ?
                WHERE file_path = ?
                """,
                (1 if teams else 0, 1 if email else 0, file_path),
            )

    def mark_failed(self, file_path: str, error: str):
        """Mark a file as failed with truncated error message."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE dbo.meetings SET status = N'failed', error_message = ? WHERE file_path = ?",
                (error[:2000], file_path),
            )

    # ── Read operations ───────────────────────────────────────

    def is_processed(self, file_path: str, file_hash: str = None) -> bool:
        """Return True only if this exact file content was successfully completed before."""
        with self._conn() as conn:
            if file_hash is not None:
                cursor = conn.execute(
                    "SELECT 1 FROM dbo.meetings WHERE file_path = ? AND file_hash = ? AND status = N'completed'",
                    (file_path, file_hash),
                )
            else:
                cursor = conn.execute(
                    "SELECT 1 FROM dbo.meetings WHERE file_path = ? AND status = N'completed'",
                    (file_path,),
                )
            return cursor.fetchone() is not None

    def get_mom(self, file_path: str) -> Optional[Dict]:
        """Retrieve previously generated MOM JSON for a file."""
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT mom_json FROM dbo.meetings WHERE file_path = ?",
                (file_path,),
            )
            row = cursor.fetchone()
            if row and row[0]:
                return json.loads(row[0])
        return None

    def get_recent_meetings(self, limit: int = 20) -> List[Dict]:
        """Return recent meetings for status dashboard."""
        with self._conn() as conn:
            cursor = conn.execute(
                """
                SELECT TOP(?) id, file_name, meeting_title, meeting_date, status,
                              processed_at, teams_notified, email_sent, error_message
                FROM dbo.meetings
                ORDER BY processed_at DESC
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            return [self._to_dict(cursor, row) for row in rows]

    def get_failed_meetings(self) -> List[Dict]:
        """Return all failed meetings for retry."""
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT file_path, file_name, error_message FROM dbo.meetings WHERE status = N'failed'"
            )
            rows = cursor.fetchall()
            return [self._to_dict(cursor, row) for row in rows]

    # ── Feature additions ─────────────────────────────────────

    def update_sentiment(self, file_path: str, sentiment: Dict):
        """Store sentiment analysis result (Feature 8 — Morale Detection)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE dbo.meetings SET sentiment_json = ? WHERE file_path = ?",
                (json.dumps(sentiment, ensure_ascii=False), file_path),
            )

    def set_awaiting_approval(self, file_path: str, token: str):
        """Mark meeting as awaiting organizer approval before team delivery (Feature 9)."""
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE dbo.meetings
                SET approval_token = ?, approval_status = N'pending', status = N'awaiting_approval'
                WHERE file_path = ?
                """,
                (token, file_path),
            )

    def get_pending_approval_by_token(self, token: str) -> Optional[Dict]:
        """Return meeting details for a pending approval token (Feature 9)."""
        with self._conn() as conn:
            cursor = conn.execute(
                """
                SELECT file_path, mom_json, meeting_title
                FROM dbo.meetings
                WHERE approval_token = ? AND approval_status = N'pending'
                """,
                (token,),
            )
            row = cursor.fetchone()
            return self._to_dict(cursor, row) if row else None

    def set_approval_result(self, file_path: str, result: str):
        """Record the approval decision and update meeting status accordingly (Feature 9)."""
        new_status = "completed" if result in ("approved", "auto_approved") else "rejected"
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE dbo.meetings
                SET approval_status = ?, status = ?
                WHERE file_path = ?
                """,
                (result, new_status, file_path),
            )

    def get_meetings_by_date_range(self, start_date: str, end_date: str) -> List[Dict]:
        """Return completed meetings within an inclusive date range YYYY-MM-DD (Feature 10)."""
        with self._conn() as conn:
            cursor = conn.execute(
                """
                SELECT id, file_name, meeting_title, meeting_date,
                       mom_json, attendance_json
                FROM dbo.meetings
                WHERE status = N'completed'
                  AND meeting_date BETWEEN ? AND ?
                ORDER BY meeting_date ASC
                """,
                (start_date, end_date),
            )
            rows = cursor.fetchall()
            return [self._to_dict(cursor, row) for row in rows]

