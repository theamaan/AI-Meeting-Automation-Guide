"""
tests/test_teams_notifier.py
Unit tests for Teams Adaptive Card builder.
No network calls required — all tests check card structure.
Run: pytest tests/ -v
"""

import pytest
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from teams_notifier import TeamsNotifier


@pytest.fixture
def notifier():
    return TeamsNotifier(webhook_url="https://example.webhook.office.com/test")


@pytest.fixture
def sample_mom():
    return {
        "meeting_title": "Daily Standup",
        "meeting_date": "2026-04-29",
        "overall_status": "HAS_ISSUES",
        "status_reason": "2 blockers reported",
        "team_summary": "Team made progress. Two members blocked.",
        "key_decisions": ["Demo moved to Monday"],
        "follow_up_date": None,
        "participants": [
            {
                "name": "Alice Johnson",
                "yesterday": ["Completed OAuth module"],
                "today": ["Start dashboard"],
                "blockers": [],
                "action_items": ["Send demo recording by EOD"],
                "progress_summary": "On track."
            },
            {
                "name": "Bob Smith",
                "yesterday": ["Set up payment SDK"],
                "today": ["Waiting on credentials"],
                "blockers": ["Waiting for Stripe API key"],
                "action_items": ["Follow up with vendor"],
                "progress_summary": "Blocked on vendor."
            }
        ]
    }


class TestCardStructure:

    def test_payload_is_valid_dict(self, notifier, sample_mom):
        payload = notifier._build_payload(sample_mom)
        assert isinstance(payload, dict)

    def test_payload_is_serializable(self, notifier, sample_mom):
        """Card must be JSON-serializable — Teams will reject it otherwise."""
        payload = notifier._build_payload(sample_mom)
        serialized = json.dumps(payload)
        assert len(serialized) > 100

    def test_adaptive_card_schema_present(self, notifier, sample_mom):
        payload = notifier._build_payload(sample_mom)
        card = payload["attachments"][0]["content"]
        assert card["$schema"] == "http://adaptivecards.io/schemas/adaptive-card.json"
        assert card["type"] == "AdaptiveCard"
        assert card["version"] == "1.4"

    def test_card_has_body(self, notifier, sample_mom):
        payload = notifier._build_payload(sample_mom)
        card = payload["attachments"][0]["content"]
        assert "body" in card
        assert len(card["body"]) > 0

    def test_card_has_full_width(self, notifier, sample_mom):
        payload = notifier._build_payload(sample_mom)
        card = payload["attachments"][0]["content"]
        assert card.get("msteams", {}).get("width") == "Full"

    def test_expand_all_action_present(self, notifier, sample_mom):
        payload = notifier._build_payload(sample_mom)
        card = payload["attachments"][0]["content"]
        actions = card.get("actions", [])
        assert len(actions) >= 1
        assert actions[0]["type"] == "Action.ToggleVisibility"

    def test_expand_all_targets_all_participants(self, notifier, sample_mom):
        payload = notifier._build_payload(sample_mom)
        card = payload["attachments"][0]["content"]
        expand_action = card["actions"][0]
        targets = expand_action["targetElements"]
        assert "details_alice_johnson" in targets
        assert "details_bob_smith" in targets

    def test_detail_containers_exist_and_hidden(self, notifier, sample_mom):
        """Each participant should have a hidden detail container."""
        payload = notifier._build_payload(sample_mom)
        card = payload["attachments"][0]["content"]
        body = card["body"]

        container_ids = [
            item.get("id") for item in body
            if item.get("type") == "Container" and "id" in item
        ]
        assert "details_alice_johnson" in container_ids
        assert "details_bob_smith" in container_ids

        for item in body:
            if item.get("id") in ("details_alice_johnson", "details_bob_smith"):
                assert item.get("isVisible") is False

    def test_has_issues_banner_present(self, notifier, sample_mom):
        """Issues banner should appear when overall_status is HAS_ISSUES."""
        payload = notifier._build_payload(sample_mom)
        serialized = json.dumps(payload)
        assert "2 blockers reported" in serialized

    def test_all_clear_no_issues_banner(self, notifier, sample_mom):
        mom = dict(sample_mom)
        mom["overall_status"] = "ALL_CLEAR"
        mom["status_reason"] = ""
        payload = notifier._build_payload(mom)
        serialized = json.dumps(payload)
        # Status reason should not appear for ALL_CLEAR
        assert "2 blockers reported" not in serialized

    def test_blocker_person_has_red_indicator(self, notifier, sample_mom):
        """Bob has blockers — should see 🔴 in card."""
        payload = notifier._build_payload(sample_mom)
        serialized = json.dumps(payload)
        assert "🔴" in serialized

    def test_clear_person_has_green_indicator(self, notifier, sample_mom):
        """Alice has no blockers — should see 🟢 in card."""
        payload = notifier._build_payload(sample_mom)
        serialized = json.dumps(payload)
        assert "🟢" in serialized

    def test_key_decisions_in_card(self, notifier, sample_mom):
        payload = notifier._build_payload(sample_mom)
        serialized = json.dumps(payload)
        assert "Demo moved to Monday" in serialized

    def test_empty_participants_no_crash(self, notifier):
        mom = {
            "meeting_title": "Empty Meeting",
            "meeting_date": "2026-01-01",
            "overall_status": "ALL_CLEAR",
            "status_reason": "",
            "team_summary": "No participants.",
            "key_decisions": [],
            "participants": []
        }
        payload = notifier._build_payload(mom)
        assert isinstance(payload, dict)


class TestSafeId:

    def test_safe_id_basic(self):
        notifier = TeamsNotifier("http://test")
        assert notifier._safe_id("Alice Johnson") == "alice_johnson"

    def test_safe_id_special_chars(self):
        notifier = TeamsNotifier("http://test")
        result = notifier._safe_id("Jean-Pierre Müller")
        assert " " not in result
        assert "-" not in result

    def test_safe_id_numbers(self):
        notifier = TeamsNotifier("http://test")
        result = notifier._safe_id("User123")
        assert result == "user123"
