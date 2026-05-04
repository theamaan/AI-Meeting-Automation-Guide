"""
tests/test_llm_engine.py
Unit tests for LLM engine — JSON extraction, validation, fallback logic.
No Ollama connection required (all LLM calls are mocked).
Run: pytest tests/ -v
"""

import pytest
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from llm_engine import OllamaLLMEngine


@pytest.fixture
def engine():
    return OllamaLLMEngine(model="test-model", base_url="http://localhost:11434")


class TestJsonExtraction:

    def test_clean_json(self, engine):
        raw = '{"meeting_title": "Standup", "participants": []}'
        result = engine._extract_json(raw)
        assert result["meeting_title"] == "Standup"

    def test_strips_markdown_fence(self, engine):
        raw = '```json\n{"meeting_title": "Standup"}\n```'
        result = engine._extract_json(raw)
        assert result["meeting_title"] == "Standup"

    def test_strips_preamble(self, engine):
        raw = 'Here is the JSON you requested:\n\n{"meeting_title": "Standup"}'
        result = engine._extract_json(raw)
        assert result["meeting_title"] == "Standup"

    def test_strips_postamble(self, engine):
        raw = '{"meeting_title": "Standup"}\n\nI hope this helps!'
        result = engine._extract_json(raw)
        assert result["meeting_title"] == "Standup"

    def test_repairs_trailing_comma(self, engine):
        raw = '{"items": ["a", "b",]}'
        result = engine._extract_json(raw)
        assert result["items"] == ["a", "b"]

    def test_raises_on_no_json(self, engine):
        with pytest.raises(json.JSONDecodeError):
            engine._extract_json("This is just plain text with no JSON.")

    def test_nested_json(self, engine):
        raw = '{"outer": {"inner": "value"}, "list": [1, 2, 3]}'
        result = engine._extract_json(raw)
        assert result["outer"]["inner"] == "value"
        assert result["list"] == [1, 2, 3]


class TestValidationAndRepair:

    def test_adds_missing_top_level_keys(self, engine):
        data = {"participants": []}
        repaired = engine._validate_and_repair(data, [], "Test", "2026-01-01")
        assert "team_summary" in repaired
        assert "overall_status" in repaired
        assert "key_decisions" in repaired

    def test_fixes_invalid_status(self, engine):
        data = {
            "overall_status": "CLEAR",   # Wrong value
            "participants": []
        }
        repaired = engine._validate_and_repair(data, [], "Test", "2026-01-01")
        assert repaired["overall_status"] in ("ALL_CLEAR", "HAS_ISSUES")

    def test_adds_missing_participants(self, engine):
        data = {
            "participants": [
                {"name": "Alice", "yesterday": [], "today": [],
                 "blockers": [], "action_items": [], "progress_summary": ""}
            ]
        }
        repaired = engine._validate_and_repair(
            data, ["Alice", "Bob"], "Test", "2026-01-01"
        )
        names = [p["name"] for p in repaired["participants"]]
        assert "Alice" in names
        assert "Bob" in names  # Was missing, should be added

    def test_drops_unexpected_participants_when_expected_list_provided(self, engine):
        data = {
            "participants": [
                {"name": "Alice", "yesterday": [], "today": [],
                 "blockers": [], "action_items": [], "progress_summary": ""},
                {"name": "Mallory", "yesterday": [], "today": [],
                 "blockers": [], "action_items": [], "progress_summary": ""}
            ]
        }
        repaired = engine._validate_and_repair(
            data, ["Alice", "Bob"], "Test", "2026-01-01"
        )
        names = [p["name"] for p in repaired["participants"]]
        assert names == ["Alice", "Bob"]

    def test_normalises_string_to_list(self, engine):
        """If LLM returns a string instead of a list, validate should handle it."""
        data = {
            "participants": [
                {
                    "name": "Alice",
                    "yesterday": "Completed login",   # String, not list
                    "today": [],
                    "blockers": [],
                    "action_items": [],
                    "progress_summary": ""
                }
            ]
        }
        # The current implementation will catch non-list with isinstance check
        repaired = engine._validate_and_repair(data, ["Alice"], "Test", "2026-01-01")
        alice = repaired["participants"][0]
        # After repair, yesterday should be a list
        assert isinstance(alice["yesterday"], list)

    def test_participant_gets_default_fields(self, engine):
        data = {"participants": [{"name": "Bob"}]}  # Missing all fields
        repaired = engine._validate_and_repair(data, ["Bob"], "Test", "2026-01-01")
        bob = repaired["participants"][0]
        assert "yesterday" in bob
        assert "blockers" in bob
        assert isinstance(bob["blockers"], list)


class TestFallbackMOM:

    def test_fallback_structure(self, engine):
        result = engine._fallback_mom(
            ["Alice", "Bob"], "2026-01-01", "Daily Standup"
        )
        assert result["overall_status"] == "HAS_ISSUES"
        assert len(result["participants"]) == 2
        names = [p["name"] for p in result["participants"]]
        assert "Alice" in names
        assert "Bob" in names

    def test_fallback_with_empty_participants(self, engine):
        result = engine._fallback_mom([], "2026-01-01", "Test")
        # Should use ["Unknown"] as fallback
        assert len(result["participants"]) == 1
        assert result["participants"][0]["name"] == "Unknown"


class TestTruncation:

    def test_short_transcript_not_truncated(self, engine):
        text = "A" * 100
        result = engine._truncate_transcript(text, max_chars=200)
        assert result == text

    def test_long_transcript_truncated(self, engine):
        text = "A" * 20000
        result = engine._truncate_transcript(text, max_chars=5000)
        assert len(result) < 20000
        assert "omitted" in result

    def test_truncated_keeps_start_and_end(self, engine):
        text = "START" + ("X" * 20000) + "END"
        result = engine._truncate_transcript(text, max_chars=1000)
        assert "START" in result
        assert "END" in result
