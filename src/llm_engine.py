"""
llm_engine.py — Ollama LLM Integration
Sends transcript to Ollama (default: kimi-k2-thinking:cloud) and returns
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
    "Do NOT output your reasoning, thinking process, or hidden analysis. "
    "Start your response with { and end with }. "
    "Never hallucinate. Only use information explicitly stated in the transcript."
)

MOM_PROMPT_TEMPLATE = """Analyze this meeting transcript and produce a structured Minutes of Meeting (MOM) report.

MEETING TITLE: {meeting_title}
MEETING DATE: {meeting_date}
KNOWN PARTICIPANTS: {participants}

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
        model: str = "kimi-k2-thinking:cloud",
        base_url: str = "http://localhost:11434",
        temperature: float = 0.1,
        max_retries: int = 3,
        timeout: int = 600,
        max_transcript_chars: int = 140000,
        num_predict: int = 12288,
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
    ) -> Dict:
        """
        Call LLM to extract structured MOM from transcript.
        Returns validated dict or a safe fallback on failure.
        """
        prompt = MOM_PROMPT_TEMPLATE.format(
            transcript=self._truncate_transcript(transcript_text, self.max_transcript_chars),
            participants=", ".join(participants) if participants else "Not identified",
            meeting_date=meeting_date or "Not specified",
            meeting_title=meeting_title or "Team Meeting",
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
                "stop": [
                    "\n```",
                    "```\n",
                    "\n\nNote:",
                    "\n\nExplanation:",
                    "<think>",
                    "</think>",
                ],
            },
        }
        response = requests.post(self.generate_url, json=payload, timeout=self.timeout)
        response.raise_for_status()
        return response.json().get("response", "").strip()

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
