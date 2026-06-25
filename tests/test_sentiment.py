"""
test_sentiment.py — Unit tests for Feature 8: Sentiment & Morale Signal Detection

All tests mock Ollama — no running LLM connection required.
Run: pytest tests/test_sentiment.py -v
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from llm_engine import OllamaLLMEngine


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture()
def engine():
    return OllamaLLMEngine(model="test-model", base_url="http://localhost:11434")


PARTICIPANTS = ["Alice Johnson", "Bob Smith", "Carol White"]

VALID_SENTIMENT_RESPONSE = {
    "team_morale": "Team is generally positive with one concern.",
    "flags_count": 1,
    "participants": [
        {"name": "Alice Johnson",  "tone": "confident",  "confidence_score": 0.9, "signals": ["spoke clearly"], "flag_for_followup": False},
        {"name": "Bob Smith",      "tone": "frustrated",  "confidence_score": 0.85, "signals": ["mentioned blocker twice"], "flag_for_followup": True},
        {"name": "Carol White",    "tone": "neutral",    "confidence_score": 0.5, "signals": [],                   "flag_for_followup": False},
    ],
}


# ──────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────

class TestAnalyzeSentiment:

    def test_returns_valid_schema_on_success(self, engine):
        """analyze_sentiment returns a dict with all required top-level keys."""
        raw = json.dumps(VALID_SENTIMENT_RESPONSE)
        with patch.object(engine, "_call_ollama", return_value=raw):
            result = engine.analyze_sentiment("transcript text", PARTICIPANTS)

        assert "team_morale"   in result
        assert "flags_count"   in result
        assert "participants"  in result
        assert isinstance(result["participants"], list)
        assert len(result["participants"]) == len(PARTICIPANTS)

    def test_flags_count_matches_actual_flags(self, engine):
        """flags_count is recomputed from actual data, not trusted from LLM."""
        # LLM claims 0 flags but Bob has flag_for_followup=True
        tampered = dict(VALID_SENTIMENT_RESPONSE)
        tampered["flags_count"] = 0   # deliberately wrong
        raw = json.dumps(tampered)
        with patch.object(engine, "_call_ollama", return_value=raw):
            result = engine.analyze_sentiment("transcript", PARTICIPANTS)
        # Validator must correct this to 1
        assert result["flags_count"] == 1

    def test_missing_participant_is_added_as_neutral(self, engine):
        """A whitelist participant not returned by LLM is inserted with neutral defaults."""
        partial = {
            "team_morale": "Partial data.",
            "flags_count": 0,
            "participants": [
                {"name": "Alice Johnson", "tone": "confident", "confidence_score": 0.8,
                 "signals": [], "flag_for_followup": False},
                # Bob and Carol missing from LLM response
            ],
        }
        with patch.object(engine, "_call_ollama", return_value=json.dumps(partial)):
            result = engine.analyze_sentiment("transcript", PARTICIPANTS)

        names = [p["name"] for p in result["participants"]]
        assert "Bob Smith"   in names
        assert "Carol White" in names

    def test_invalid_tone_is_replaced_with_neutral(self, engine):
        """An invalid tone string (e.g. 'angry') is normalised to 'neutral'."""
        invalid = {
            "team_morale": "OK.",
            "flags_count": 0,
            "participants": [
                {"name": "Alice Johnson", "tone": "angry",  "confidence_score": 0.8, "signals": [], "flag_for_followup": False},
                {"name": "Bob Smith",     "tone": "happy",  "confidence_score": 0.6, "signals": [], "flag_for_followup": False},
                {"name": "Carol White",   "tone": "neutral","confidence_score": 0.5, "signals": [], "flag_for_followup": False},
            ],
        }
        with patch.object(engine, "_call_ollama", return_value=json.dumps(invalid)):
            result = engine.analyze_sentiment("transcript", PARTICIPANTS)

        tones = {p["name"]: p["tone"] for p in result["participants"]}
        assert tones["Alice Johnson"] == "neutral"
        assert tones["Bob Smith"]     == "neutral"

    def test_fallback_returned_on_all_retries_failed(self, engine):
        """Returns all-neutral fallback when every LLM attempt fails."""
        engine.max_retries = 1
        with patch.object(engine, "_call_ollama", side_effect=Exception("LLM down")):
            result = engine.analyze_sentiment("transcript", PARTICIPANTS)

        assert result["flags_count"] == 0
        tones = {p["name"]: p["tone"] for p in result["participants"]}
        for name in PARTICIPANTS:
            assert tones.get(name) == "neutral"

    def test_confidence_score_clamped_to_valid_range(self, engine):
        """confidence_score outside [0,1] is clamped."""
        out_of_range = {
            "team_morale": "OK.",
            "flags_count": 0,
            "participants": [
                {"name": "Alice Johnson", "tone": "confident", "confidence_score": 5.0, "signals": [], "flag_for_followup": False},
                {"name": "Bob Smith",     "tone": "neutral",   "confidence_score": -1.0,"signals": [], "flag_for_followup": False},
                {"name": "Carol White",   "tone": "neutral",   "confidence_score": 0.5, "signals": [], "flag_for_followup": False},
            ],
        }
        with patch.object(engine, "_call_ollama", return_value=json.dumps(out_of_range)):
            result = engine.analyze_sentiment("transcript", PARTICIPANTS)

        scores = {p["name"]: p["confidence_score"] for p in result["participants"]}
        assert 0.0 <= scores["Alice Johnson"] <= 1.0
        assert 0.0 <= scores["Bob Smith"]     <= 1.0


class TestFallbackSentiment:

    def test_fallback_all_neutral_no_flags(self, engine):
        result = engine._fallback_sentiment(PARTICIPANTS)
        assert result["flags_count"] == 0
        assert all(p["tone"] == "neutral" for p in result["participants"])
        assert all(p["flag_for_followup"] is False for p in result["participants"])

    def test_fallback_includes_all_participants(self, engine):
        result = engine._fallback_sentiment(PARTICIPANTS)
        names = {p["name"] for p in result["participants"]}
        assert names == set(PARTICIPANTS)

    def test_fallback_empty_participants(self, engine):
        result = engine._fallback_sentiment([])
        assert result["participants"] == []
        assert result["flags_count"]  == 0
