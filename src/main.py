"""
main.py — AI Meeting Intelligence System Entry Point
Orchestrates: File Watcher → Parser → LLM → Teams → Email → DB

Usage:
    python main.py                         # Start background watcher
    python main.py --file path/to/file.vtt # Process single file
    python main.py --status                # Show recent processing log
    python main.py --retry-failed          # Retry all failed files
    python main.py --config custom.yaml    # Use alternate config
"""

import argparse
import hashlib
import logging
import re
import secrets
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

# Add src/ to path when running directly
sys.path.insert(0, str(Path(__file__).parent))

from approval_server import ApprovalCallbackServer
from config import load_config
from database import Database
from digest import WeeklyDigestService
from emailer import EmailService
from llm_engine import OllamaLLMEngine
from parser import TranscriptParser
from teams_notifier import TeamsNotifier
from watcher import FileWatcher
from attendance_parser import find_attendance_csv, parse_attendance_csv, classify_attendance


# ──────────────────────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────────────────────

def setup_logging(log_level: str, log_file: str):
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s"
    handlers = [
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format=fmt,
        handlers=handlers,
    )


# ──────────────────────────────────────────────────────────────
# System
# ──────────────────────────────────────────────────────────────

class MeetingIntelligenceSystem:
    """
    Central orchestrator.
    Each component is independently testable and replaceable.
    """

    def __init__(self, config_path: str = "config/settings.yaml"):
        self.config = load_config(config_path)
        setup_logging(self.config.log_level, self.config.log_file)
        self.logger = logging.getLogger("MeetingSystem")

        # Core components
        self.db     = Database(self.config.db_path)
        self.parser = TranscriptParser()
        self.llm    = OllamaLLMEngine(
            model                = self.config.ollama.model,
            base_url             = self.config.ollama.base_url,
            temperature          = self.config.ollama.temperature,
            max_retries          = self.config.ollama.max_retries,
            timeout              = self.config.ollama.timeout,
            max_transcript_chars = getattr(self.config.ollama, "max_transcript_chars", 30000),
            num_predict          = getattr(self.config.ollama, "num_predict", 8192),
        )

        # Optional notification channels
        self.teams   = None
        self.emailer = None

        if self.config.teams.enabled and self.config.teams.webhook_url:
            self.teams = TeamsNotifier(self.config.teams.webhook_url)
        else:
            self.logger.info("Teams notifications disabled (no webhook URL configured).")

        if self.config.email.enabled and self.config.email.username:
            self.emailer = EmailService(
                smtp_host   = self.config.email.smtp_host,
                smtp_port   = self.config.email.smtp_port,
                username    = self.config.email.username,
                password    = self.config.email.password,
                use_tls     = self.config.email.use_tls,
                sender_name = self.config.email.sender_name,
            )
        else:
            self.logger.info("Email notifications disabled (no SMTP username configured).")

        # Feature 9: Approval gate server (localhost only, daemon thread)
        self.approval_server = None
        if self.config.approval.enabled:
            self.approval_server = ApprovalCallbackServer(
                db=self.db,
                port=self.config.approval.callback_port,
            )
            self.approval_server.start()
            self.logger.info(
                "Approval server listening on http://127.0.0.1:%d",
                self.config.approval.callback_port,
            )
        else:
            self.logger.info("Approval gate disabled.")

        self.watcher  = None
        self._running = False

    # ── Main processing pipeline ──────────────────────────────

    def process_recording(self, file_path: str):
        """
        Full pipeline: parse → LLM → Teams → Email → DB
        All steps are independently error-handled so a failure
        in one channel does not block the others.
        """
        self.logger.info("=" * 60)
        self.logger.info("Processing: %s", Path(file_path).name)
        self.logger.info("=" * 60)

        file_type = Path(file_path).suffix.lower()
        try:
            with open(file_path, "rb") as fh:
                file_hash = hashlib.md5(fh.read()).hexdigest()
        except OSError:
            file_hash = None
        self.db.record_meeting(file_path, file_type, file_hash)

        try:
            # ── Step 1: Parse / Transcribe ────────────────────
            self.logger.info("[1/4] Parsing transcript...")
            transcript = self.parser.parse(file_path)
            self.db.update_transcript(file_path, transcript.raw_text)

            speakers = self.parser.get_speakers(transcript)
            self.logger.info(
                "      Segments: %d  |  Speakers: %s",
                len(transcript.segments),
                speakers or ["(none identified)"],
            )

            participants = self.config.expected_participants or speakers
            if self.config.expected_participants:
                self.logger.info(
                    "      Using configured attendee whitelist (%d): %s",
                    len(self.config.expected_participants),
                    self.config.expected_participants,
                )

            meeting_title, meeting_date = self._extract_metadata(file_path)

            # ── Step 1b: Attendance tracking ──────────────────
            attendance_result = None
            att_csv = find_attendance_csv(file_path)
            if att_csv:
                att_report = parse_attendance_csv(att_csv)
                if att_report:
                    attendance_result = classify_attendance(
                        whitelist=participants,
                        csv_attendees=att_report.attendees,
                        transcript_speakers=speakers,
                    )
                    self.db.update_attendance(file_path, attendance_result)
                    self.logger.info(
                        "      Attendance (CSV ✔): %d spoke  |  %d silent  |  %d absent",
                        len(attendance_result["spoke"]),
                        len(attendance_result["silent"]),
                        len(attendance_result["absent"]),
                    )
                else:
                    self.logger.warning("      Attendance CSV found but could not be parsed.")
            else:
                self.logger.info("      No attendance CSV found — transcript-only (no absent labels).")

            if attendance_result is None:
                # Fallback: transcript speakers only — never labels anyone absent
                attendance_result = classify_attendance(
                    whitelist=participants,
                    csv_attendees=None,
                    transcript_speakers=speakers,
                )
            self.logger.info("[2/4] Running LLM analysis (model: %s)...", self.config.ollama.model)
            mom_data = self.llm.generate_mom(
                transcript_text = transcript.raw_text,
                participants    = participants,
                meeting_date    = meeting_date,
                meeting_title   = meeting_title,
                attendance      = attendance_result,
            )
            self.db.update_mom(file_path, mom_data, meeting_title, meeting_date)
            self.logger.info(
                "      MOM generated for %d participant(s)  |  Status: %s",
                len(mom_data.get("participants", [])),
                mom_data.get("overall_status", "?"),
            )

            # ── Step 2b: Sentiment analysis (confidential, manager-only) ─
            sentiment_data = None
            if self.config.sentiment.enabled:
                self.logger.info("[2b] Running sentiment analysis...")
                sentiment_data = self.llm.analyze_sentiment(
                    transcript_text=transcript.raw_text,
                    participants=participants,
                    meeting_title=meeting_title,
                    meeting_date=meeting_date,
                )
                self.db.update_sentiment(file_path, sentiment_data)
                self.logger.info(
                    "      Sentiment complete  |  Flags: %d",
                    sentiment_data.get("flags_count", 0),
                )
            else:
                self.logger.info("[2b] Sentiment analysis disabled.")

            # ── Approval gate (Feature 9) ──────────────────────
            if (
                self.config.approval.enabled
                and self.approval_server
                and self.config.approval.organizer_email
                and self.emailer
            ):
                decision = self._request_approval(
                    file_path, mom_data, meeting_title, meeting_date
                )
                if decision == "rejected":
                    self.logger.warning(
                        "MOM rejected by organizer — pipeline halted for: %s",
                        Path(file_path).name,
                    )
                    return
                # "approved" or "auto_approved" → fall through to delivery steps

            # ── Step 3: Teams notification ─────────────────
            teams_sent = False
            if self.teams:
                self.logger.info("[3/5] Sending Teams Adaptive Card...")
                teams_sent = self.teams.send_mom_card(mom_data, attendance=attendance_result)
                if teams_sent:
                    self.logger.info("      Teams webhook accepted payload (delivery may be asynchronous).")
                else:
                    self.logger.warning("      Teams webhook call failed — check URL / firewall / flow configuration.")
            else:
                self.logger.info("[3/5] Teams skipped (not configured.)")

            # ── Step 4: Email ─────────────────────────────────
            email_sent = False
            if self.emailer and self.config.email.recipients:
                self.logger.info("[4/5] Sending email to %d recipient(s)...", len(self.config.email.recipients))
                email_sent = self.emailer.send_mom_email(
                    mom_data   = mom_data,
                    recipients = self.config.email.recipients,
                    attendance = attendance_result,
                )
                if email_sent:
                    self.logger.info("      Email delivered.")
                else:
                    self.logger.warning("      Email delivery failed — check SMTP config.")
            else:
                self.logger.info("[4/5] Email skipped (not configured or no recipients.)")

            self.db.mark_notification_sent(file_path, teams=teams_sent, email=email_sent)

            # ── Step 5: Manager morale report (confidential) ─────
            if (
                self.config.sentiment.enabled
                and sentiment_data is not None
                and self.emailer
                and self.config.sentiment.manager_email
            ):
                self.logger.info("[5/5] Sending confidential morale report to manager...")
                sent = self.emailer.send_sentiment_email(
                    sentiment_data=sentiment_data,
                    mom_data=mom_data,
                    manager_email=self.config.sentiment.manager_email,
                )
                if sent:
                    self.logger.info("      Manager morale report delivered.")
                else:
                    self.logger.warning("      Manager morale report delivery failed.")
            else:
                self.logger.info("[5/5] Manager morale report skipped (disabled or not configured).")

            self.logger.info("✓ Completed: %s", Path(file_path).name)

        except Exception as exc:
            self.logger.error(
                "✗ Pipeline failed for %s: %s",
                Path(file_path).name,
                exc,
                exc_info=True,
            )
            self.db.mark_failed(file_path, str(exc))

    # ── Watcher mode ──────────────────────────────────────────

    def start_watcher(self):
        """Start background file watcher — blocks until SIGINT/SIGTERM."""
        self._validate_config()

        self.logger.info("Starting AI Meeting Intelligence System")
        self.logger.info("  Watch path : %s", self.config.watcher.watch_path)
        self.logger.info("  Model      : %s", self.config.ollama.model)
        self.logger.info("  Delay      : %ds", self.config.watcher.delay_seconds)
        self.logger.info("  Teams      : %s", "enabled" if self.teams else "disabled")
        self.logger.info("  Email      : %s", "enabled" if self.emailer else "disabled")

        self.watcher = FileWatcher(
            watch_path     = self.config.watcher.watch_path,
            callback       = self.process_recording,
            delay_seconds  = self.config.watcher.delay_seconds,
            db             = self.db,
        )

        self._running = True
        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        self.watcher.start()
        self.logger.info("Watcher running. Press Ctrl+C to stop.")

        while self._running:
            time.sleep(1)

    def _shutdown(self, signum, frame):
        self.logger.info("Shutting down gracefully...")
        self._running = False
        if self.watcher:
            self.watcher.stop()
        if self.approval_server:
            self.approval_server.stop()
        sys.exit(0)

    # ── Utilities ─────────────────────────────────────────────

    def _validate_config(self):
        if not self.config.watcher.watch_path:
            self.logger.error("WATCH_PATH not configured. Set in config/settings.yaml or env var.")
            sys.exit(1)

        watch_dir = Path(self.config.watcher.watch_path)
        if not watch_dir.exists():
            self.logger.warning("Watch path does not exist — creating: %s", watch_dir)
            watch_dir.mkdir(parents=True, exist_ok=True)

        if not self.llm.health_check():
            self.logger.error(
                "Ollama is not running or model unavailable.\n"
                "  Start Ollama  : ollama serve\n"
                "  Pull model    : ollama pull %s",
                self.config.ollama.model,
            )
            sys.exit(1)

    def _extract_metadata(self, file_path: str):
        """
        Infer meeting title and date from filename.
        Teams recording names follow patterns like:
          "Daily Standup-20240415_143022-Meeting Recording.mp4"
          "Sprint Review_20240429.vtt"
        """
        stem = Path(file_path).stem
        date_match = re.search(r"(\d{4})(\d{2})(\d{2})", stem)

        if date_match:
            y, m, d = date_match.groups()
            meeting_date = f"{y}-{m}-{d}"
            raw_title = stem[: date_match.start()]
        else:
            meeting_date = datetime.now().strftime("%Y-%m-%d")
            raw_title = stem

        # Clean up title
        meeting_title = re.sub(r"[-_]+", " ", raw_title).strip()
        meeting_title = re.sub(r"\s+", " ", meeting_title)
        meeting_title = meeting_title.title() or "Team Meeting"

        return meeting_title, meeting_date

    def print_status(self, limit: int = 15):
        """Print recent processing status to stdout."""
        meetings = self.db.get_recent_meetings(limit)
        if not meetings:
            print("No meetings processed yet.")
            return

        print(f"\n{'─'*80}")
        print(f"  {'File':<35} {'Status':<12} {'Date':<12} {'Teams':<7} {'Email'}")
        print(f"{'─'*80}")
        for m in meetings:
            status  = m["status"].upper()
            teams   = "✓" if m["teams_notified"] else "✗"
            email   = "✓" if m["email_sent"] else "✗"
            fname   = (m["file_name"] or "")[:34]
            date    = (m["meeting_date"] or "")[:10]
            print(f"  {fname:<35} {status:<12} {date:<12} {teams:<7} {email}")

        failed = self.db.get_failed_meetings()
        if failed:
            print(f"\n  ⚠  {len(failed)} failed file(s) — run with --retry-failed")
        print(f"{'─'*80}\n")

    def retry_failed(self):
        """Re-process all files marked as failed."""
        failed = self.db.get_failed_meetings()
        if not failed:
            print("No failed files to retry.")
            return
        self.logger.info("Retrying %d failed file(s)...", len(failed))
        for record in failed:
            fp = record["file_path"]
            if Path(fp).exists():
                self.process_recording(fp)
            else:
                self.logger.warning("File no longer exists: %s", fp)

    def _request_approval(
        self,
        file_path: str,
        mom_data: dict,
        meeting_title: str,
        meeting_date: str,
    ) -> str:
        """
        Send draft MOM to organizer and wait for approval/rejection.
        Returns: 'approved', 'rejected', or 'auto_approved'.
        Feature 9 — Draft Approval Gate.
        """
        token       = secrets.token_urlsafe(32)
        base_url    = f"http://127.0.0.1:{self.config.approval.callback_port}"
        approve_url = f"{base_url}/approve?token={token}"
        reject_url  = f"{base_url}/reject?token={token}"

        self.db.set_awaiting_approval(file_path, token)

        self.emailer.send_draft_approval_email(
            mom_data=mom_data,
            organizer_email=self.config.approval.organizer_email,
            approve_url=approve_url,
            reject_url=reject_url,
            timeout_minutes=self.config.approval.timeout_minutes,
        )
        self.logger.info(
            "Draft sent to organizer (%s). Waiting up to %d min...",
            self.config.approval.organizer_email,
            self.config.approval.timeout_minutes,
        )

        result = self.approval_server.wait_for_decision(
            token=token,
            timeout_seconds=self.config.approval.timeout_minutes * 60,
            auto_approve=self.config.approval.auto_approve,
        )
        self.db.set_approval_result(file_path, result)
        return result

    def run_weekly_digest(self, days_back: int = None) -> None:
        """
        Send a personalised weekly productivity digest to every participant
        listed in config.digest.participant_emails (Feature 10).

        Args:
            days_back: Override the days_back setting from settings.yaml.
        """
        if not self.config.digest.participant_emails:
            self.logger.error(
                "digest.participant_emails is not configured in settings.yaml. "
                "Add a name: email mapping for each participant who should receive a digest."
            )
            return
        if not self.emailer:
            self.logger.error(
                "Email is not configured (EMAIL_USERNAME / EMAIL_PASSWORD not set). "
                "Cannot send weekly digest."
            )
            return

        self.logger.info(
            "Starting weekly digest for %d participant(s)...",
            len(self.config.digest.participant_emails),
        )
        svc = WeeklyDigestService(
            config=self.config,
            db=self.db,
            llm=self.llm,
            emailer=self.emailer,
        )
        results = svc.run(days_back=days_back)
        for name, ok in results.items():
            status = "✓" if ok else "✗ FAILED"
            self.logger.info("  Digest %-35s %s", name, status)

        sent  = sum(1 for v in results.values() if v)
        total = len(results)
        self.logger.info("Weekly digest complete: %d/%d sent.", sent, total)


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="AI Meeting Intelligence System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                                # Start background watcher
  python main.py --file recording.vtt          # Process one file now
  python main.py --status                      # Show processing history
  python main.py --retry-failed                # Retry failed files
  python main.py --config custom.yaml          # Use custom config
        """,
    )
    ap.add_argument("--config",        default="config/settings.yaml", help="Path to settings YAML")
    ap.add_argument("--file",          metavar="FILE",  help="Process a single file immediately")
    ap.add_argument("--status",        action="store_true",            help="Show recent status")
    ap.add_argument("--retry-failed",  action="store_true",            help="Retry all failed files")
    ap.add_argument("--weekly-digest", action="store_true",
                    help="Send personal weekly digest emails to all configured participants")
    ap.add_argument("--digest-days",   type=int, default=None, metavar="N",
                    help="Override days_back for digest (default: from settings.yaml)")
    args = ap.parse_args()

    system = MeetingIntelligenceSystem(config_path=args.config)

    if args.file:
        system.process_recording(args.file)
    elif args.status:
        system.print_status()
    elif args.retry_failed:
        system.retry_failed()
    elif args.weekly_digest:
        system.run_weekly_digest(days_back=args.digest_days)
    else:
        system.start_watcher()


if __name__ == "__main__":
    main()

