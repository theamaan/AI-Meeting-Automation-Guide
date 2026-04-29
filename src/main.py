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
import logging
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

# Add src/ to path when running directly
sys.path.insert(0, str(Path(__file__).parent))

from config import load_config
from database import Database
from emailer import EmailService
from llm_engine import OllamaLLMEngine
from parser import TranscriptParser
from teams_notifier import TeamsNotifier
from watcher import FileWatcher


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
            model       = self.config.ollama.model,
            base_url    = self.config.ollama.base_url,
            temperature = self.config.ollama.temperature,
            max_retries = self.config.ollama.max_retries,
            timeout     = self.config.ollama.timeout,
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
        self.db.record_meeting(file_path, file_type)

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

            meeting_title, meeting_date = self._extract_metadata(file_path)

            # ── Step 2: LLM Analysis ──────────────────────────
            self.logger.info("[2/4] Running LLM analysis (model: %s)...", self.config.ollama.model)
            mom_data = self.llm.generate_mom(
                transcript_text = transcript.raw_text,
                participants    = speakers,
                meeting_date    = meeting_date,
                meeting_title   = meeting_title,
            )
            self.db.update_mom(file_path, mom_data, meeting_title, meeting_date)
            self.logger.info(
                "      MOM generated for %d participant(s)  |  Status: %s",
                len(mom_data.get("participants", [])),
                mom_data.get("overall_status", "?"),
            )

            # ── Step 3: Teams notification ────────────────────
            teams_sent = False
            if self.teams:
                self.logger.info("[3/4] Sending Teams Adaptive Card...")
                teams_sent = self.teams.send_mom_card(mom_data)
                if teams_sent:
                    self.logger.info("      Teams card delivered.")
                else:
                    self.logger.warning("      Teams delivery failed — check webhook URL / firewall.")
            else:
                self.logger.info("[3/4] Teams skipped (not configured).")

            # ── Step 4: Email ─────────────────────────────────
            email_sent = False
            if self.emailer and self.config.email.recipients:
                self.logger.info("[4/4] Sending email to %d recipient(s)...", len(self.config.email.recipients))
                email_sent = self.emailer.send_mom_email(
                    mom_data   = mom_data,
                    recipients = self.config.email.recipients,
                )
                if email_sent:
                    self.logger.info("      Email delivered.")
                else:
                    self.logger.warning("      Email delivery failed — check SMTP config.")
            else:
                self.logger.info("[4/4] Email skipped (not configured or no recipients).")

            self.db.mark_notification_sent(file_path, teams=teams_sent, email=email_sent)
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
    args = ap.parse_args()

    system = MeetingIntelligenceSystem(config_path=args.config)

    if args.file:
        system.process_recording(args.file)
    elif args.status:
        system.print_status()
    elif args.retry_failed:
        system.retry_failed()
    else:
        system.start_watcher()


if __name__ == "__main__":
    main()
