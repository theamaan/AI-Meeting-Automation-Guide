"""
llm_engine.py — Ollama LLM Integration
Sends transcript to Ollama (default: gpt-oss:120b-cloud) and returns
a validated, structured MOM JSON per team member.

Key design:
  - Temperature 0.1 for deterministic output
  - Strict JSON-only system prompt
  - Response cleaning + JSON boundary extraction
  - Schema validation before accepting output
  - 3-attempt retry with exponential backoff
  - Safe fallback MOM if all retries fail
"""

import json
import logging
import re
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are an expert enterprise meeting analyst. "
    "Your ONLY job is to output valid JSON. "
    "Do NOT output any explanation, markdown, code blocks, or extra text. "
    "Start your response with { and end with }. "
    "Never hallucinate. Only use information explicitly stated in the transcript."
)

MOM_PROMPT_TEMPLATE = """Analyze this meeting transcript and produce a structured Minutes of Meeting (MOM) report.

MEETING TITLE: {meeting_title}
MEETING DATE: {meeting_date}
KNOWN PARTICIPANTS: {participants}

ATTENDANCE STATUS:
{attendance_context}

─── TRANSCRIPT ───
{transcript}
─────────────────

STRICT EXTRACTION RULES:
1. Extract per participant: yesterday, today, blockers, action_items, progress_summary
   - yesterday  : what they said they completed or were working on previously
   - today      : what they said they will do or are currently doing
   - blockers   : any impediments, blockers, waiting-ons, or risks they raised
   - action_items: specific tasks assigned TO them (include deadline if mentioned)
   - progress_summary: 2-3 factual sentences about their status
2. If a participant did not speak or was not mentioned, include them with all empty arrays.
3. Do NOT infer. Do NOT fill in gaps. Use [] for missing data.
4. Keep each list item under 20 words.
5. overall_status = "ALL_CLEAR" if no blockers exist across all participants, else "HAS_ISSUES"
6. key_decisions = specific decisions made in the meeting (not action items)
7. ATTENDANCE RULES (apply only when attendance data is provided above):
   - ABSENT participants : set progress_summary = "Absent from this meeting." and all arrays to []
   - SILENT participants : set progress_summary = "Attended but did not speak." and all arrays to []
   - Do NOT assume anyone is absent unless they are explicitly listed as ABSENT above

OUTPUT — return ONLY this JSON, nothing else:
{{
  "meeting_title": "{meeting_title}",
  "meeting_date": "{meeting_date}",
  "overall_status": "ALL_CLEAR",
  "status_reason": "",
  "team_summary": "2-3 sentence team-wide summary",
  "key_decisions": ["decision1"],
  "follow_up_date": null,
  "participants": [
    {{
      "name": "Full Name",
      "yesterday": ["item"],
      "today": ["item"],
      "blockers": [],
      "action_items": ["task — by date"],
      "progress_summary": "2-3 sentences"
    }}
  ]
}}

Return ONLY the JSON object. Begin with {{ and end with }}."""


# ──────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────

class OllamaLLMEngine:

    def __init__(
        self,
        model: str = "gpt-oss:120b-cloud",
        base_url: str = "http://localhost:11434",
        temperature: float = 0.1,
        max_retries: int = 3,
        timeout: int = 450,
        max_transcript_chars: int = 140000,
        num_predict: int = 20000,  # Cloud proxy maps to max_tokens — must be positive (-1 is rejected by cloud models)
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.generate_url = f"{self.base_url}/api/generate"
        self.temperature = temperature
        self.max_retries = max_retries
        self.timeout = timeout
        self.max_transcript_chars = max_transcript_chars
        self.num_predict = num_predict

    # ── Public ────────────────────────────────────────────────

    def health_check(self) -> bool:
        """Verify Ollama is running and the model is available."""
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=10)
            if resp.status_code != 200:
                return False
            available = [m["name"] for m in resp.json().get("models", [])]
            if self.model not in available:
                logger.warning(
                    f"Model '{self.model}' not found in Ollama. "
                    f"Available: {available}. "
                    f"Pull with: ollama pull {self.model}"
                )
                # Still return True — model might be available but listed differently
            return True
        except requests.exceptions.ConnectionError:
            logger.error("Ollama is not reachable at %s. Run: ollama serve", self.base_url)
            return False
        except Exception as exc:
            logger.error("Ollama health check error: %s", exc)
            return False

    def generate_mom(
        self,
        transcript_text: str,
        participants: List[str],
        meeting_date: str = "",
        meeting_title: str = "",
        attendance: Optional[Dict] = None,
    ) -> Dict:
        """
        Call LLM to extract structured MOM from transcript.
        Returns validated dict or a safe fallback on failure.
        """
        attendance_context = self._format_attendance_context(attendance)
        prompt = MOM_PROMPT_TEMPLATE.format(
            transcript=self._truncate_transcript(transcript_text, self.max_transcript_chars),
            participants=", ".join(participants) if participants else "Not identified",
            meeting_date=meeting_date or "Not specified",
            meeting_title=meeting_title or "Team Meeting",
            attendance_context=attendance_context,
        )

        for attempt in range(1, self.max_retries + 1):
            logger.info("LLM attempt %d/%d", attempt, self.max_retries)
            try:
                raw = self._call_ollama(prompt)
                logger.debug("Raw LLM output (first 500 chars): %s", raw[:500])
                parsed = self._extract_json(raw)
                validated = self._validate_and_repair(parsed, participants, meeting_title, meeting_date)
                logger.info("LLM succeeded on attempt %d", attempt)
                return validated
            except json.JSONDecodeError as exc:
                logger.warning("JSON parse failed attempt %d: %s", attempt, exc)
                logger.warning(
                    "Raw LLM output that failed parsing (first 800 chars): %s",
                    repr(raw[:800]) if raw else "<empty — model returned nothing>",
                )
            except requests.exceptions.Timeout:
                logger.warning("LLM timeout on attempt %d (timeout=%ds)", attempt, self.timeout)
            except Exception as exc:
                logger.error("LLM error attempt %d: %s", attempt, exc, exc_info=True)

            if attempt < self.max_retries:
                wait = 5 * attempt  # 5s, 10s backoff
                logger.info("Retrying in %ds...", wait)
                time.sleep(wait)

        logger.error("All LLM attempts failed. Using fallback MOM.")
        return self._fallback_mom(participants, meeting_date, meeting_title)

    # ── Ollama API ────────────────────────────────────────────

    def _call_ollama(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": SYSTEM_PROMPT,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": self.temperature,
                "top_p": 0.9,
                "top_k": 40,
                "num_predict": self.num_predict,
                "repeat_penalty": 1.1,
                # Stop sequences prevent the model from rambling after the JSON
                "stop": ["\n```", "```\n", "\n\nNote:", "\n\nExplanation:"],
            },
        }
        response = requests.post(self.generate_url, json=payload, timeout=self.timeout)

        if not response.ok:
            # Log the actual Ollama error body before raising so we can diagnose it.
            # Without this, HTTPError only shows the status code — not the reason.
            try:
                err_body = response.json()
                err_detail = err_body.get("error", response.text[:500])
            except Exception:
                err_detail = response.text[:500]
            logger.error(
                "Ollama returned HTTP %d. Error detail: %s",
                response.status_code,
                err_detail,
            )
            response.raise_for_status()

        body = response.json()

        # done_reason tells us WHY the model stopped:
        #   "stop"   — model finished cleanly (ideal)
        #   "length" — hit num_predict token cap → output is truncated
        #   "load"   — model was still loading (shouldn’t happen with stream=False)
        done_reason = body.get("done_reason", "unknown")
        raw = body.get("response", "").strip()

        if done_reason == "length":
            logger.warning(
                "LLM hit num_predict token cap (%d). Output was truncated. "
                "Increase num_predict or reduce transcript length. "
                "Truncated output will fail JSON parsing.",
                self.num_predict,
            )
        elif done_reason not in ("stop", "unknown"):
            logger.warning("Unexpected LLM done_reason: '%s'", done_reason)

        if not raw:
            logger.warning(
                "LLM returned empty response (done_reason=%s). "
                "This usually means the model hit its token cap or had a server-side error.",
                done_reason,
            )

        return raw

    # ── JSON extraction ───────────────────────────────────────

    def _extract_json(self, raw: str) -> Dict:
        """
        Robustly extract the JSON object from LLM output.
        Handles common failure modes:
          - Markdown code fences  ```json ... ```
          - Leading explanation text before {
          - Trailing text after }
        """
        text = raw.strip()

        # Strip markdown fences
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
        text = text.strip()

        # Find outermost JSON object boundaries
        start = text.find("{")
        # rfind the matching closing brace (not just the last char)
        end = self._find_matching_brace(text, start)

        if start == -1 or end == -1:
            raise json.JSONDecodeError("No JSON object found in response", text, 0)

        json_str = text[start : end + 1]

        # Fix common LLM JSON mistakes
        json_str = self._repair_json(json_str)

        return json.loads(json_str)

    def _find_matching_brace(self, text: str, start: int) -> int:
        """Find the index of the closing } that matches the { at `start`."""
        if start == -1:
            return -1
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return i
        return -1

    def _repair_json(self, text: str) -> str:
        """Fix common LLM JSON issues without a full parser."""
        # Trailing commas before ] or } — e.g., ["item",]
        text = re.sub(r",\s*([}\]])", r"\1", text)
        # Single quotes used instead of double quotes (best-effort)
        # Only fix simple cases to avoid breaking content with apostrophes
        # text = text.replace("'", '"')  # Too dangerous — skip
        return text

    # ── Validation ────────────────────────────────────────────

    def _validate_and_repair(
        self,
        data: Dict,
        expected_participants: List[str],
        meeting_title: str,
        meeting_date: str,
    ) -> Dict:
        """
        Validate structure and fill gaps:
        - Ensure required top-level keys exist
        - Ensure every expected participant appears in output
        - Normalize field types
        """
        # Top-level required keys with defaults
        data.setdefault("meeting_title", meeting_title or "Team Meeting")
        data.setdefault("meeting_date", meeting_date or "")
        data.setdefault("overall_status", "ALL_CLEAR")
        data.setdefault("status_reason", "")
        data.setdefault("team_summary", "")
        data.setdefault("key_decisions", [])
        data.setdefault("follow_up_date", None)
        data.setdefault("participants", [])

        # Validate overall_status value
        if data["overall_status"] not in ("ALL_CLEAR", "HAS_ISSUES"):
            # Fuzzy fix
            if any(p.get("blockers") for p in data["participants"]):
                data["overall_status"] = "HAS_ISSUES"
            else:
                data["overall_status"] = "ALL_CLEAR"

        # Ensure participants is a list
        if not isinstance(data["participants"], list):
            data["participants"] = []

        expected_names = [name.strip() for name in expected_participants if name and name.strip()]
        expected_names_lower = {name.lower() for name in expected_names}

        if expected_names_lower:
            filtered_participants = []
            for person in data["participants"]:
                person_name = str(person.get("name", "")).strip()
                if person_name.lower() in expected_names_lower:
                    filtered_participants.append(person)
                else:
                    logger.debug("Dropping unexpected participant from MOM: %s", person_name or "<blank>")
            data["participants"] = filtered_participants

        # Normalise each participant entry
        present_names_lower = {p.get("name", "").lower() for p in data["participants"]}
        for person in data["participants"]:
            person.setdefault("name", "Unknown")
            for key in ("yesterday", "today", "blockers", "action_items"):
                if not isinstance(person.get(key), list):
                    person[key] = []
            person.setdefault("progress_summary", "")

        # Add missing participants with empty data
        for name in expected_names:
            if name.lower() not in present_names_lower:
                logger.debug("Adding missing participant to MOM: %s", name)
                data["participants"].append(
                    {
                        "name": name,
                        "yesterday": [],
                        "today": [],
                        "blockers": [],
                        "action_items": [],
                        "progress_summary": "No updates recorded for this participant.",
                    }
                )

        return data

    # ── Utilities ─────────────────────────────────────────────

    def _format_attendance_context(self, attendance: Optional[Dict]) -> str:
        """Format attendance dict as a readable block for the LLM prompt."""
        if not attendance:
            return "No attendance report available — use transcript speakers as a guide only. Do NOT label anyone as absent."
        lines = []
        if attendance.get("has_csv"):
            if attendance.get("spoke"):
                lines.append(f"  PRESENT (spoke):   {', '.join(attendance['spoke'])}")
            if attendance.get("silent"):
                lines.append(f"  PRESENT (silent):  {', '.join(attendance['silent'])}")
            if attendance.get("absent"):
                lines.append(f"  ABSENT:            {', '.join(attendance['absent'])}")
            lines.append("  (Source: official Teams attendance report — authoritative)")
        else:
            if attendance.get("spoke"):
                lines.append(f"  Confirmed present (spoke in transcript): {', '.join(attendance['spoke'])}")
            if attendance.get("unknown"):
                lines.append(f"  Status unknown (no attendance CSV):      {', '.join(attendance['unknown'])}")
            lines.append("  WARNING: No attendance CSV available — do NOT assume unknowns are absent.")
        return "\n".join(lines) if lines else "No attendance data."

    def _truncate_transcript(self, text: str, max_chars: int = 30000) -> str:
        """
        Preserve the start and end of the transcript when truncating.
        Most important context is at the beginning (intros, agenda) and
        end (action items, wrap-up).
        """
        if len(text) <= max_chars:
            return text

        keep = max_chars // 2
        omitted = len(text) - max_chars
        truncated = (
            text[:keep]
            + f"\n\n[... {omitted:,} characters omitted for context window ...]\n\n"
            + text[-keep:]
        )
        logger.warning(
            "Transcript truncated: %d → ~%d chars (%d omitted)",
            len(text),
            max_chars,
            omitted,
        )
        return truncated

    def _fallback_mom(
        self,
        participants: List[str],
        meeting_date: str,
        meeting_title: str,
    ) -> Dict:
        """Return a valid-structure MOM indicating processing failure."""
        return {
            "meeting_title": meeting_title or "Team Meeting",
            "meeting_date": meeting_date or "",
            "overall_status": "HAS_ISSUES",
            "status_reason": "LLM processing failed — manual review required",
            "team_summary": (
                "Automated MOM generation failed. "
                "Please review the transcript manually."
            ),
            "key_decisions": [],
            "follow_up_date": None,
            "participants": [
                {
                    "name": p,
                    "yesterday": [],
                    "today": [],
                    "blockers": ["LLM processing failed — manual review required"],
                    "action_items": [],
                    "progress_summary": (
                        "Automated processing failed for this participant. "
                        "Review transcript manually."
                    ),
                }
                for p in (participants or ["Unknown"])
            ],
        }


# ──────────────────────────────────────────────────────────────
# Sentiment analysis prompt (Feature 8)
# ──────────────────────────────────────────────────────────────

SENTIMENT_PROMPT_TEMPLATE = """Analyze the tone and morale signals in this meeting transcript.

MEETING TITLE: {meeting_title}
MEETING DATE: {meeting_date}
KNOWN PARTICIPANTS: {participants}

─── TRANSCRIPT ───
{transcript}
─────────────────

STRICT RULES:
1. Analyse ONLY what is explicitly present in the transcript — do NOT infer or assume.
2. For each participant, identify tone signals (specific phrases or patterns that indicate their emotional state).
3. tone must be exactly one of: "confident", "frustrated", "uncertain", "disengaged", "neutral"
4. confidence_score: 0.0 to 1.0 reflecting how strongly the evidence supports the tone label.
5. flag_for_followup: true ONLY when tone is "frustrated" or "disengaged" AND evidence is strong (confidence_score >= 0.7).
6. flags_count must equal the exact count of participants where flag_for_followup is true.
7. If a participant did not speak or has insufficient data, set tone="neutral", confidence_score=0.3, signals=[], flag_for_followup=false.
8. team_morale: one concise sentence summarising the overall team tone.

OUTPUT — return ONLY this JSON, nothing else:
{{
  "team_morale": "one-sentence team-level observation",
  "flags_count": 0,
  "participants": [
    {{
      "name": "Full Name",
      "tone": "neutral",
      "confidence_score": 0.5,
      "signals": ["phrase or pattern from transcript"],
      "flag_for_followup": false
    }}
  ]
}}

Return ONLY the JSON object. Begin with {{ and end with }}."""


# Extend OllamaLLMEngine with sentiment + digest methods
# These are defined as module-level functions that are bound to the class
# to keep the class definition unmodified except by monkey-patching at import time.
# Instead, we subclass — but to avoid breaking existing usages, we extend in-place
# by adding the methods directly to OllamaLLMEngine via assignment after class definition.

def _analyze_sentiment(
    self,
    transcript_text: str,
    participants: List[str],
    meeting_title: str = "",
    meeting_date: str = "",
) -> Dict:
    """
    Run a second LLM pass to detect per-person tone and morale signals.
    Returns a validated sentiment dict or a safe all-neutral fallback.
    Feature 8 — Morale Signal Detection.
    """
    prompt = SENTIMENT_PROMPT_TEMPLATE.format(
        transcript=self._truncate_transcript(transcript_text, self.max_transcript_chars),
        participants=", ".join(participants) if participants else "Not identified",
        meeting_title=meeting_title or "Team Meeting",
        meeting_date=meeting_date or "Not specified",
    )

    for attempt in range(1, self.max_retries + 1):
        logger.info("Sentiment analysis attempt %d/%d", attempt, self.max_retries)
        try:
            raw = self._call_ollama(prompt)
            parsed = self._extract_json(raw)
            validated = self._validate_sentiment(parsed, participants)
            logger.info("Sentiment analysis succeeded on attempt %d", attempt)
            return validated
        except json.JSONDecodeError as exc:
            logger.warning("Sentiment JSON parse failed attempt %d: %s", attempt, exc)
        except requests.exceptions.Timeout:
            logger.warning("Sentiment LLM timeout on attempt %d", attempt)
        except Exception as exc:
            logger.error("Sentiment LLM error attempt %d: %s", attempt, exc, exc_info=True)

        if attempt < self.max_retries:
            time.sleep(5 * attempt)

    logger.error("All sentiment attempts failed. Using neutral fallback.")
    return self._fallback_sentiment(participants)


def _validate_sentiment(self, data: Dict, participants: List[str]) -> Dict:
    """Validate sentiment JSON structure and fill gaps."""
    valid_tones = {"confident", "frustrated", "uncertain", "disengaged", "neutral"}

    data.setdefault("team_morale", "No morale data available.")
    data.setdefault("participants", [])
    data.setdefault("flags_count", 0)

    if not isinstance(data["participants"], list):
        data["participants"] = []

    # Normalise each participant entry
    existing_names_lower = {}
    clean_participants = []
    for person in data["participants"]:
        name = str(person.get("name", "")).strip()
        if not name:
            continue
        person["name"] = name
        # Clamp tone to valid set
        if person.get("tone") not in valid_tones:
            person["tone"] = "neutral"
        # Clamp confidence_score to [0.0, 1.0]
        try:
            person["confidence_score"] = max(0.0, min(1.0, float(person.get("confidence_score", 0.5))))
        except (TypeError, ValueError):
            person["confidence_score"] = 0.5
        person.setdefault("signals", [])
        if not isinstance(person["signals"], list):
            person["signals"] = []
        person.setdefault("flag_for_followup", False)
        existing_names_lower[name.lower()] = True
        clean_participants.append(person)
    data["participants"] = clean_participants

    # Add missing whitelist members with neutral defaults
    for name in (participants or []):
        if name.strip().lower() not in existing_names_lower:
            data["participants"].append({
                "name": name.strip(),
                "tone": "neutral",
                "confidence_score": 0.3,
                "signals": [],
                "flag_for_followup": False,
            })

    # Recompute flags_count from actual data (don't trust LLM's count)
    data["flags_count"] = sum(
        1 for p in data["participants"] if p.get("flag_for_followup") is True
    )
    return data


def _fallback_sentiment(self, participants: List[str]) -> Dict:
    """Return an all-neutral, no-flags sentiment result for use when LLM fails."""
    return {
        "team_morale": "Sentiment analysis unavailable — LLM processing failed.",
        "flags_count": 0,
        "participants": [
            {
                "name": p,
                "tone": "neutral",
                "confidence_score": 0.5,
                "signals": [],
                "flag_for_followup": False,
            }
            for p in (participants or [])
        ],
    }


def _generate_digest_narrative(
    self,
    participant_name: str,
    data: Dict,
) -> str:
    """
    Generate a 2-3 sentence plain-text narrative of a participant's week.
    Uses temperature 0.3 for natural prose (not JSON).
    Feature 10 — Personal Weekly Digest.
    """
    prompt = (
        f"Write a 2-3 sentence professional, factual summary of this person's work week.\n"
        f"Base it ONLY on the data provided. Do NOT add advice, judgement, or assumptions.\n\n"
        f"Person: {participant_name}\n"
        f"Meetings attended: {data.get('meetings_attended', 0)} of {data.get('total_meetings', 0)}\n"
        f"Meetings absent: {data.get('meetings_absent', 0)}\n"
        f"Action items this week: {len(data.get('action_items', []))}\n"
        f"Blockers raised: {len(data.get('blockers', []))}\n"
        f"Recurring blockers: {', '.join(data.get('recurring_blockers', [])) or 'None'}\n\n"
        f"Write ONLY the narrative paragraph. No bullet points, no JSON, no headers."
    )

    # Use a slightly higher temperature for natural prose
    original_temp = self.temperature
    payload = {
        "model": self.model,
        "prompt": prompt,
        "system": (
            "You are a professional meeting analyst. "
            "Write factual, concise, third-person narrative summaries. "
            "Do NOT output JSON, markdown, or bullet points."
        ),
        "stream": False,
        "options": {
            "temperature": 0.3,
            "top_p": 0.9,
            "num_predict": 200,
        },
    }
    try:
        response = requests.post(self.generate_url, json=payload, timeout=60)
        if response.ok:
            text = response.json().get("response", "").strip()
            if text:
                return text
    except Exception as exc:
        logger.warning("Digest narrative LLM call failed for %s: %s", participant_name, exc)

    attended = data.get("meetings_attended", 0)
    total = data.get("total_meetings", 0)
    return (
        f"{participant_name} participated in {attended} of {total} meeting(s) this week. "
        f"{len(data.get('action_items', []))} action item(s) were recorded."
    )


# Bind the new methods to OllamaLLMEngine without altering the class body
OllamaLLMEngine.analyze_sentiment = _analyze_sentiment
OllamaLLMEngine._validate_sentiment = _validate_sentiment
OllamaLLMEngine._fallback_sentiment = _fallback_sentiment
OllamaLLMEngine.generate_digest_narrative = _generate_digest_narrative
