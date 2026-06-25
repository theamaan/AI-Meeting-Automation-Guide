"""
test_digest.py — Unit tests for Feature 10: Personal Weekly Digest

All tests mock Ollama and SMTP — no live connections required.
Run: pytest tests/test_digest.py -v
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from digest import WeeklyDigestService


# ──────────────────────────────────────────────────────────────
# Helpers / Fixtures
# ──────────────────────────────────────────────────────────────

PARTICIPANTS = ["Alice Johnson", "Bob Smith"]

def _make_mom(name: str, today=None, action_items=None, blockers=None) -> dict:
    return {
        "meeting_title": "Daily Standup",
        "meeting_date":  "2026-06-23",
        "overall_status": "ALL_CLEAR",
        "team_summary": "Good progress.",
        "key_decisions": [],
        "participants": [
            {
                "name":            name,
                "yesterday":       ["completed task A"],
                "today":           today or ["work on feature B"],
                "blockers":        blockers or [],
                "action_items":    action_items or [],
                "progress_summary": "On track.",
            }
        ],
    }


def _make_attendance(spoke=None, silent=None, absent=None, has_csv=True) -> dict:
    return {
        "has_csv": has_csv,
        "spoke":   spoke  or [],
        "silent":  silent or [],
        "absent":  absent or [],
        "unknown": [],
    }


def _make_meeting(
    title: str,
    date: str,
    mom_data: dict,
    attendance: dict = None,
) -> dict:
    return {
        "id":             1,
        "file_name":      f"{title}.vtt",
        "meeting_title":  title,
        "meeting_date":   date,
        "mom_json":       json.dumps(mom_data),
        "attendance_json": json.dumps(attendance) if attendance else None,
    }


@pytest.fixture()
def mock_config():
    cfg = MagicMock()
    cfg.digest.days_back = 7
    cfg.digest.participant_emails = {
        "Alice Johnson": "alice@example.com",
        "Bob Smith":     "bob@example.com",
    }
    return cfg


@pytest.fixture()
def mock_db():
    return MagicMock()


@pytest.fixture()
def mock_llm():
    llm = MagicMock()
    llm.generate_digest_narrative.return_value = "Alice had a productive week."
    return llm


@pytest.fixture()
def mock_emailer():
    emailer = MagicMock()
    emailer.send_digest_email.return_value = True
    return emailer


@pytest.fixture()
def svc(mock_config, mock_db, mock_llm, mock_emailer):
    return WeeklyDigestService(mock_config, mock_db, mock_llm, mock_emailer)


# ──────────────────────────────────────────────────────────────
# Tests: _build_participant_data
# ──────────────────────────────────────────────────────────────

class TestBuildParticipantData:

    def test_counts_attended_meetings_correctly(self, svc):
        """meetings_attended counts meetings where participant was present."""
        meetings = [
            _make_meeting("Standup", "2026-06-20", _make_mom("Alice Johnson")),
            _make_meeting("Standup", "2026-06-21", _make_mom("Alice Johnson")),
            _make_meeting("Standup", "2026-06-22", _make_mom("Alice Johnson")),
        ]
        data = svc._build_participant_data("Alice Johnson", meetings)
        assert data["meetings_attended"] == 3
        assert data["meetings_absent"]   == 0
        assert data["total_meetings"]    == 3

    def test_counts_absent_meetings_correctly(self, svc):
        """meetings_absent counts meetings where attendance_json marks participant absent."""
        att_absent = _make_attendance(absent=["Alice Johnson"], has_csv=True)
        meetings = [
            _make_meeting("Standup", "2026-06-20", _make_mom("Alice Johnson")),
            _make_meeting("Standup", "2026-06-21", _make_mom("Alice Johnson"),
                          attendance=att_absent),
        ]
        data = svc._build_participant_data("Alice Johnson", meetings)
        assert data["meetings_attended"] == 1
        assert data["meetings_absent"]   == 1

    def test_action_items_aggregated_across_meetings(self, svc):
        """All action items from all meetings are collected."""
        meetings = [
            _make_meeting("Standup", "2026-06-20",
                          _make_mom("Alice Johnson", action_items=["task A", "task B"])),
            _make_meeting("Standup", "2026-06-21",
                          _make_mom("Alice Johnson", action_items=["task C"])),
        ]
        data = svc._build_participant_data("Alice Johnson", meetings)
        texts = [item["text"] for item in data["action_items"]]
        assert "task A" in texts
        assert "task B" in texts
        assert "task C" in texts
        assert len(data["action_items"]) == 3

    def test_recurring_blockers_detected(self, svc):
        """A blocker appearing in 2+ meetings is flagged as recurring."""
        blocker = "Waiting for vendor API key"
        meetings = [
            _make_meeting("Standup", "2026-06-20",
                          _make_mom("Alice Johnson", blockers=[blocker])),
            _make_meeting("Standup", "2026-06-21",
                          _make_mom("Alice Johnson", blockers=[blocker])),
        ]
        data = svc._build_participant_data("Alice Johnson", meetings)
        assert blocker.lower() in data["recurring_blockers"]

    def test_non_recurring_blocker_not_in_recurring_list(self, svc):
        """A blocker appearing only once is NOT in recurring_blockers."""
        meetings = [
            _make_meeting("Standup", "2026-06-20",
                          _make_mom("Alice Johnson", blockers=["unique blocker"])),
        ]
        data = svc._build_participant_data("Alice Johnson", meetings)
        assert data["recurring_blockers"] == []

    def test_case_insensitive_recurring_detection(self, svc):
        """Recurring blocker detection is case-insensitive."""
        meetings = [
            _make_meeting("S1", "2026-06-20", _make_mom("Alice Johnson", blockers=["API Key Missing"])),
            _make_meeting("S2", "2026-06-21", _make_mom("Alice Johnson", blockers=["api key missing"])),
        ]
        data = svc._build_participant_data("Alice Johnson", meetings)
        assert len(data["recurring_blockers"]) == 1


# ──────────────────────────────────────────────────────────────
# Tests: run()
# ──────────────────────────────────────────────────────────────

class TestRun:

    def test_returns_empty_dict_when_no_meetings(self, svc, mock_db):
        """Returns {} when no completed meetings exist in the date range."""
        mock_db.get_meetings_by_date_range.return_value = []
        result = svc.run(days_back=7)
        assert result == {}

    def test_returns_empty_when_no_participant_emails(self, svc, mock_config, mock_db):
        """Returns {} when participant_emails is empty."""
        mock_db.get_meetings_by_date_range.return_value = [
            _make_meeting("Standup", "2026-06-20", _make_mom("Alice Johnson"))
        ]
        mock_config.digest.participant_emails = {}
        result = svc.run(days_back=7)
        assert result == {}

    def test_send_digest_called_once_per_participant(self, svc, mock_db, mock_emailer):
        """send_digest_email is called exactly once per configured participant."""
        meetings = [
            _make_meeting("Standup", "2026-06-20", _make_mom("Alice Johnson")),
        ]
        mock_db.get_meetings_by_date_range.return_value = meetings
        svc.run(days_back=7)
        # Two participants configured in mock_config fixture
        assert mock_emailer.send_digest_email.call_count == 2

    def test_result_dict_maps_name_to_bool(self, svc, mock_db, mock_emailer):
        """run() returns {name: bool} for each configured participant."""
        mock_db.get_meetings_by_date_range.return_value = [
            _make_meeting("Standup", "2026-06-20", _make_mom("Alice Johnson"))
        ]
        mock_emailer.send_digest_email.return_value = True
        result = svc.run(days_back=7)
        assert isinstance(result, dict)
        for name in ["Alice Johnson", "Bob Smith"]:
            assert name in result
            assert result[name] is True

    def test_failed_email_recorded_as_false(self, svc, mock_db, mock_emailer):
        """If send_digest_email returns False, result maps that participant to False."""
        mock_db.get_meetings_by_date_range.return_value = [
            _make_meeting("Standup", "2026-06-20", _make_mom("Alice Johnson"))
        ]
        mock_emailer.send_digest_email.return_value = False
        result = svc.run(days_back=7)
        for name in result:
            assert result[name] is False

    def test_days_back_override_is_respected(self, svc, mock_db, mock_emailer):
        """run(days_back=1) queries only 1 day, not the default 7."""
        mock_db.get_meetings_by_date_range.return_value = []
        svc.run(days_back=1)
        call_args = mock_db.get_meetings_by_date_range.call_args
        start_date, end_date = call_args[0]
        from datetime import datetime, timedelta
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt   = datetime.strptime(end_date,   "%Y-%m-%d")
        delta = (end_dt - start_dt).days
        assert delta == 1


# ──────────────────────────────────────────────────────────────
# Tests: static helpers
# ──────────────────────────────────────────────────────────────

class TestStaticHelpers:

    def test_parse_json_field_valid(self):
        raw = json.dumps({"key": "value"})
        assert WeeklyDigestService._parse_json_field(raw) == {"key": "value"}

    def test_parse_json_field_none_returns_empty(self):
        assert WeeklyDigestService._parse_json_field(None) == {}

    def test_parse_json_field_invalid_returns_empty(self):
        assert WeeklyDigestService._parse_json_field("not-json") == {}

    def test_is_absent_with_csv_and_match(self):
        att = {"has_csv": True, "absent": ["Alice Johnson"]}
        assert WeeklyDigestService._is_absent("alice johnson", att) is True

    def test_is_absent_no_csv(self):
        att = {"has_csv": False, "absent": ["Alice Johnson"]}
        assert WeeklyDigestService._is_absent("alice johnson", att) is False

    def test_is_absent_not_in_list(self):
        att = {"has_csv": True, "absent": ["Bob Smith"]}
        assert WeeklyDigestService._is_absent("alice johnson", att) is False

    def test_find_participant_case_insensitive(self):
        mom = {"participants": [{"name": "Alice Johnson", "today": ["task"]}]}
        result = WeeklyDigestService._find_participant("alice johnson", mom)
        assert result is not None
        assert result["name"] == "Alice Johnson"

    def test_find_participant_not_found_returns_none(self):
        mom = {"participants": [{"name": "Bob Smith"}]}
        assert WeeklyDigestService._find_participant("alice johnson", mom) is None

    def test_find_recurring_blockers_two_meetings(self):
        blockers = [["api key", "network issue"], ["api key"]]
        result = WeeklyDigestService._find_recurring_blockers(blockers)
        assert "api key" in result
        assert "network issue" not in result

    def test_find_recurring_blockers_single_meeting_returns_empty(self):
        result = WeeklyDigestService._find_recurring_blockers([["blocker"]])
        assert result == []
