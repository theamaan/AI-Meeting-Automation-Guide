"""
migrate_sqlite_to_sqlserver.py
==============================
One-time data migration: copies all rows from the SQLite meetings.db
into the SQL Server MeetingSystem database.

Run this AFTER:
  1. scripts/create_sqlserver_schema.sql has been executed in SSMS
  2. config/settings.yaml database section is correctly filled in
  3. pyodbc is installed (pip install pyodbc)

Usage (from the project root):
  python scripts/migrate_sqlite_to_sqlserver.py
  python scripts/migrate_sqlite_to_sqlserver.py --sqlite-path data/meetings.db
  python scripts/migrate_sqlite_to_sqlserver.py --dry-run       # preview without writing
"""

import argparse
import json
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

# Allow importing from src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pyodbc
from config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("migration")


# ────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────

def parse_date(value) -> "date | None":
    """Convert SQLite TEXT date (YYYY-MM-DD) or None to Python date."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def coerce_bit(value) -> int:
    """Convert SQLite INTEGER 0/1 or None to 0/1 for SQL Server BIT."""
    return 1 if value else 0


def load_sqlite_rows(sqlite_path: str):
    """Read all rows from the SQLite meetings table as plain dicts."""
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute("""
            SELECT
                id, file_path, file_name, file_type, file_hash,
                processed_at, transcript, mom_json, attendance_json,
                sentiment_json, meeting_date, meeting_title,
                status, error_message, teams_notified, email_sent,
                approval_token, approval_status
            FROM meetings
            ORDER BY id ASC
        """)
        rows = [dict(row) for row in cursor.fetchall()]
        logger.info("Loaded %d rows from SQLite (%s)", len(rows), sqlite_path)
        return rows
    except sqlite3.OperationalError as exc:
        logger.error("Could not read SQLite database: %s", exc)
        raise
    finally:
        conn.close()


def validate_status(status: str) -> str:
    """Map any status value not in the SQL Server CHECK constraint to 'failed'."""
    allowed = {"pending", "processing", "completed", "failed", "awaiting_approval", "rejected"}
    return status if status in allowed else "failed"


def validate_approval_status(value: str) -> "str | None":
    allowed = {"not_required", "pending", "approved", "rejected", "auto_approved", "auto_rejected"}
    if value is None:
        return None
    return value if value in allowed else None


# ────────────────────────────────────────────────────────────────
# Migration
# ────────────────────────────────────────────────────────────────

_INSERT_SQL = """
IF NOT EXISTS (SELECT 1 FROM dbo.meetings WHERE file_path = ?)
BEGIN
    INSERT INTO dbo.meetings (
        file_path, file_name, file_type, file_hash,
        processed_at, transcript, mom_json, attendance_json,
        sentiment_json, meeting_date, meeting_title,
        status, error_message, teams_notified, email_sent,
        approval_token, approval_status
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
END
"""


def migrate(sqlite_path: str, connection_string: str, dry_run: bool = False):
    rows = load_sqlite_rows(sqlite_path)

    if not rows:
        logger.info("No rows to migrate. Done.")
        return

    if dry_run:
        logger.info("[DRY RUN] Would insert %d rows — no changes written.", len(rows))
        for r in rows[:5]:
            logger.info("  Sample row id=%s  file=%s  status=%s", r["id"], r["file_name"], r["status"])
        return

    conn = pyodbc.connect(connection_string, timeout=30)
    conn.autocommit = False

    inserted = 0
    skipped = 0
    errors = 0

    try:
        cursor = conn.cursor()

        for row in rows:
            file_path = row["file_path"]
            try:
                cursor.execute(
                    _INSERT_SQL,
                    (
                        file_path,                              # WHERE check param
                        file_path,                              # INSERT params below
                        row["file_name"],
                        row["file_type"],
                        row.get("file_hash"),
                        row.get("processed_at"),                # TEXT timestamp — SQL Server accepts ISO strings
                        row.get("transcript"),
                        row.get("mom_json"),
                        row.get("attendance_json"),
                        row.get("sentiment_json"),
                        parse_date(row.get("meeting_date")),    # TEXT → DATE
                        row.get("meeting_title"),
                        validate_status(row.get("status", "failed")),
                        (row.get("error_message") or "")[:2000] or None,
                        coerce_bit(row.get("teams_notified")),
                        coerce_bit(row.get("email_sent")),
                        row.get("approval_token"),
                        validate_approval_status(row.get("approval_status")),
                    ),
                )
                # rowcount == 0 means the IF NOT EXISTS block didn't insert (already there)
                if cursor.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1

            except pyodbc.Error as exc:
                logger.warning("Row id=%s (%s) failed: %s", row["id"], row["file_name"], exc)
                errors += 1

        conn.commit()
        logger.info(
            "Migration complete — inserted: %d | skipped (already exist): %d | errors: %d",
            inserted, skipped, errors,
        )

    except Exception:
        conn.rollback()
        logger.error("Migration rolled back due to unexpected error.")
        raise
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Migrate SQLite meetings.db → SQL Server")
    parser.add_argument(
        "--sqlite-path",
        default="data/meetings.db",
        help="Path to existing SQLite database (default: data/meetings.db)",
    )
    parser.add_argument(
        "--config",
        default="config/settings.yaml",
        help="Path to settings.yaml (default: config/settings.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview migration without writing anything to SQL Server",
    )
    args = parser.parse_args()

    # Validate SQLite source
    sqlite_path = Path(args.sqlite_path)
    if not sqlite_path.exists():
        logger.error("SQLite database not found: %s", sqlite_path)
        sys.exit(1)

    # Load connection string from config
    try:
        cfg = load_config(args.config)
        connection_string = cfg.database.build_connection_string()
        logger.info("Target: Server=%s  Database=%s", cfg.database.server, cfg.database.database)
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        sys.exit(1)

    # Test SQL Server connectivity
    if not args.dry_run:
        try:
            test_conn = pyodbc.connect(connection_string, timeout=10)
            test_conn.close()
            logger.info("SQL Server connection OK.")
        except pyodbc.Error as exc:
            logger.error("Cannot connect to SQL Server: %s", exc)
            logger.error(
                "Check: server name, ODBC driver, Windows/SQL auth, firewall, "
                "and that create_sqlserver_schema.sql has been run."
            )
            sys.exit(1)

    migrate(str(sqlite_path), connection_string, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
