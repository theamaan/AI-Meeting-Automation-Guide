"""
digest.py — Personal Weekly Productivity Digest (Feature 10)

Queries completed meetings from the past N days per participant,
builds individual activity summaries, and sends personalised emails.

Run via: python src/main.py --weekly-digest
Or with override: python src/main.py --weekly-digest --digest-days 14
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class WeeklyDigestService:
    """
    Orchestrates the weekly digest pipeline for all configured participants.

    Constructor args:
        config:  AppConfig — for digest settings and participant_emails
        db:      Database  — for querying completed meetings
        llm:     OllamaLLMEngine — for generating narrative paragraphs
        emailer: EmailService — for sending individual emails
    """

    def __init__(self, config, db, llm, emailer):
        self.config  = config
        self.db      = db
        self.llm     = llm
        self.emailer = emailer

    # ── Public ────────────────────────────────────────────────

    def run(self, days_back: Optional[int] = None) -> Dict[str, bool]:
        """
        Send a personalised weekly digest to every participant in
        config.digest.participant_emails.

        Returns:
            Dict mapping participant name → True (sent) / False (failed).
        """
        effective_days = days_back if days_back is not None else self.config.digest.days_back

        end_dt   = datetime.now()
        start_dt = end_dt - timedelta(days=effective_days)
        start_date = start_dt.strftime("%Y-%m-%d")
        end_date   = end_dt.strftime("%Y-%m-%d")

        date_range = {"start": start_date, "end": end_date}

        logger.info(
            "Weekly digest: querying meetings from %s to %s (%d day(s))",
            start_date, end_date, effective_days,
        )

        meetings = self.db.get_meetings_by_date_range(start_date, end_date)

        if not meetings:
            logger.warning(
                "No completed meetings found between %s and %s — digest not sent.",
                start_date, end_date,
            )
            return {}

        logger.info("Found %d meeting(s) in range.", len(meetings))

        participant_emails = self.config.digest.participant_emails
        if not participant_emails:
            logger.error(
                "digest.participant_emails is empty in settings.yaml — "
                "no recipients configured."
            )
            return {}

        results: Dict[str, bool] = {}
        for name, email in participant_emails.items():
            logger.info("Building digest for: %s (%s)", name, email)
            try:
                data = self._build_participant_data(name, meetings)
                data["narrative"] = self.llm.generate_digest_narrative(name, data)
                ok = self.emailer.send_digest_email(
                    participant_name=name,
                    digest_data=data,
                    recipient_email=email,
                    date_range=date_range,
                )
                results[name] = ok
                if ok:
                    logger.info("  ✓ Digest sent: %s", name)
                else:
                    logger.warning("  ✗ Digest delivery failed: %s", name)
            except Exception as exc:
                logger.error("  ✗ Digest error for %s: %s", name, exc, exc_info=True)
                results[name] = False

        sent_count = sum(1 for v in results.values() if v)
        logger.info(
            "Weekly digest complete: %d/%d sent successfully.",
            sent_count, len(results),
        )
        return results

    # ── Internal ──────────────────────────────────────────────

    def _build_participant_data(self, name: str, meetings: List[Dict]) -> Dict:
        """
        Extract and aggregate per-participant data across all meetings.

        Returns a dict with:
            meetings_attended:  int
            meetings_absent:    int
            total_meetings:     int
            action_items:       List[Dict] — {text, meeting_date, meeting_title}
            blockers:           List[str]
            recurring_blockers: List[str] — blocker text appearing in >= 2 meetings
            meetings_detail:    List[Dict] — per-meeting breakdown for template
        """
        name_lower = name.strip().lower()
        total = len(meetings)
        attended = 0
        absent   = 0
        all_action_items: List[Dict] = []
        all_blockers_raw: List[str]  = []
        # Track blockers per meeting to detect recurring ones
        blockers_by_meeting: List[List[str]] = []
        meetings_detail: List[Dict] = []

        for m in meetings:
            mom_data     = self._parse_json_field(m.get("mom_json"))
            att_data     = self._parse_json_field(m.get("attendance_json"))
            meeting_date  = m.get("meeting_date", "")
            meeting_title = m.get("meeting_title", "Unknown Meeting")

            # Determine attendance status for this participant
            was_absent = self._is_absent(name_lower, att_data)

            if was_absent:
                absent += 1
                meetings_detail.append({
                    "date":       meeting_date,
                    "title":      meeting_title,
                    "was_absent": True,
                    "yesterday":  [],
                    "today":      [],
                })
                continue

            # Find participant's MOM entry
            participant_entry = self._find_participant(name_lower, mom_data)
            if participant_entry is None:
                # Present but not in MOM (silent attendee or not in whitelist)
                meetings_detail.append({
                    "date":       meeting_date,
                    "title":      meeting_title,
                    "was_absent": False,
                    "yesterday":  [],
                    "today":      [],
                })
                attended += 1
                continue

            attended += 1

            # Collect action items
            for item_text in participant_entry.get("action_items", []):
                if isinstance(item_text, str) and item_text.strip():
                    all_action_items.append({
                        "text":          item_text.strip(),
                        "meeting_date":  meeting_date,
                        "meeting_title": meeting_title,
                    })

            # Collect blockers
            meeting_blockers: List[str] = []
            for b in participant_entry.get("blockers", []):
                if isinstance(b, str) and b.strip():
                    meeting_blockers.append(b.strip())
                    all_blockers_raw.append(b.strip())
            if meeting_blockers:
                blockers_by_meeting.append(meeting_blockers)

            meetings_detail.append({
                "date":       meeting_date,
                "title":      meeting_title,
                "was_absent": False,
                "yesterday":  participant_entry.get("yesterday", []),
                "today":      participant_entry.get("today", []),
            })

        # Detect recurring blockers (same normalised text in >= 2 meetings)
        recurring = self._find_recurring_blockers(blockers_by_meeting)

        return {
            "meetings_attended":  attended,
            "meetings_absent":    absent,
            "total_meetings":     total,
            "action_items":       all_action_items,
            "blockers":           all_blockers_raw,
            "recurring_blockers": recurring,
            "meetings_detail":    meetings_detail,
            "narrative":          "",  # Filled by caller after LLM call
        }

    @staticmethod
    def _parse_json_field(raw: Optional[str]) -> Dict:
        """Safely parse a JSON string field from the database. Returns {} on failure."""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}

    @staticmethod
    def _is_absent(name_lower: str, att_data: Dict) -> bool:
        """Return True if participant is explicitly listed as absent in attendance data."""
        if not att_data or not att_data.get("has_csv"):
            return False
        absent_list = att_data.get("absent", [])
        return any(a.strip().lower() == name_lower for a in absent_list)

    @staticmethod
    def _find_participant(name_lower: str, mom_data: Dict) -> Optional[Dict]:
        """Find a participant entry in MOM JSON by name (case-insensitive)."""
        participants = mom_data.get("participants", [])
        if not isinstance(participants, list):
            return None
        for p in participants:
            if isinstance(p, dict):
                if p.get("name", "").strip().lower() == name_lower:
                    return p
        return None

    @staticmethod
    def _find_recurring_blockers(blockers_by_meeting: List[List[str]]) -> List[str]:
        """
        Return blockers that appear (by normalised text) in 2 or more meetings.
        Normalisation: lowercase + strip whitespace.
        """
        if len(blockers_by_meeting) < 2:
            return []

        # Count across meetings — only count once per meeting
        blocker_meeting_count: Dict[str, int] = {}
        for meeting_blockers in blockers_by_meeting:
            seen_this_meeting = set()
            for b in meeting_blockers:
                key = b.lower().strip()
                if key and key not in seen_this_meeting:
                    blocker_meeting_count[key] = blocker_meeting_count.get(key, 0) + 1
                    seen_this_meeting.add(key)

        return [
            b for b, count in blocker_meeting_count.items()
            if count >= 2
        ]
