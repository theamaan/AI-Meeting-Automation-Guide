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


class LLMGenerationError(RuntimeError):
    """Raised when all LLM retry attempts are exhausted and no MOM could be generated.
    Propagating this exception stops the pipeline before Teams or email are sent."""

# ──────────────────────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are an expert enterprise meeting analyst specializing in extracting "
    "complete and accurate Minutes of Meeting (MOM). "
    "Your ONLY responsibility is to analyze the transcript and return a single "
    "valid JSON object. "
    "Do NOT output explanations, markdown, code blocks, comments, or any text "
    "outside the JSON object. "
    "The response MUST begin with '{' and end with '}'. "
    "Never hallucinate, infer, assume, summarize away, or fabricate information. "
    "Only use information explicitly stated in the transcript. "
    "Capture every explicit discussion point including decisions, action items, "
    "instructions, commands, requests, questions, answers, concerns, blockers, "
    "risks, suggestions, approvals, rejections, follow-ups, reminders, next steps, "
    "assignments, deadlines, and commitments. "
    "If multiple people provide different opinions or instructions, preserve each "
    "individually instead of merging them into a summary. "
    "Do not omit details simply because they appear minor."
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
1. Extract every participant's updates exactly as discussed.
2. For every participant extract:
   - yesterday        : what they completed or were working on previously
   - today            : what they will do or are currently working on
   - blockers         : anything preventing or impeding their progress
   - action_items     : every task, request, or instruction directed at them
   - progress_summary : factual summary of their status and contributions
3. "Action Items" include:
   - tasks assigned to that participant
   - requests made to them
   - commands or instructions given to them
   - follow-up work they are responsible for
   - deliverables, investigations, documentation, testing, deployment, or review work
   - anything they were explicitly asked or instructed to do
4. Blockers include:
   - technical issues
   - waiting for approvals or dependencies
   - missing access or credentials
   - unanswered questions
   - risks and concerns
   - anything preventing progress
5. Extract ALL meeting-level information into discussion_points, including:
   - decisions made
   - instructions from managers or leads
   - commands given during the meeting
   - questions asked and answers provided
   - announcements and reminders
   - approvals and rejections
   - important technical discussions
   - follow-up topics and future plans
   - deadlines and commitments
   - any discussion that is not captured as an action item or blocker
6. Do NOT combine multiple discussion points into one summary if they were discussed separately.
7. Preserve chronological order wherever possible.
8. If multiple tasks are assigned to one participant, include every task separately.
9. Never infer missing information.
10. If something was mentioned, include it.
11. If a participant did not speak or was not mentioned, include them with empty arrays.
12. Use [] instead of null for missing lists.
13. Keep list items concise but complete — prioritize accuracy over brevity.
14. overall_status = "ALL_CLEAR" only if no blockers exist across the entire meeting.
    Otherwise use "HAS_ISSUES".
15. key_decisions must contain only decisions actually made.
    Do NOT place action items or discussion points here.
16. ATTENDANCE RULES:
    - ABSENT participants:
      progress_summary = "Absent from this meeting."
      all arrays = []
    - SILENT participants:
      progress_summary = "Attended but did not speak."
      all arrays = []
    - Never assume someone is absent unless explicitly listed as ABSENT above.

IMPORTANT:
Every explicit discussion point in the transcript should appear somewhere in the output.
Do not omit manager instructions, participant requests, commands, technical discussions,
clarifications, recommendations, reminders, decisions, or follow-up conversations simply
because they seem minor. Favor completeness over brevity while remaining factual.

OUTPUT — return ONLY this JSON, nothing else:
{{
  "meeting_title": "{meeting_title}",
  "meeting_date": "{meeting_date}",
  "overall_status": "ALL_CLEAR",
  "status_reason": "",
  "team_summary": "A factual summary covering every major discussion topic, key progress updates, important concerns, decisions, and overall direction of the meeting.",
  "discussion_points": [
    "Discussion point 1",
    "Discussion point 2"
  ],
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


# Maximum participants to process in one LLM pass.
# Meetings with more participants are split into groups so the output JSON
# stays within the num_predict token cap (13 000 tokens × 10 proxy = 130 000 max_tokens).
_MAX_PARTICIPANTS_PER_CHUNK = 10

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
        num_predict: int = 13000,
        max_output_tokens: int = 131072,  # Hard ceiling — clamp num_predict to this before every request
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.generate_url = f"{self.base_url}/api/generate"
        self.temperature = temperature
        self.max_retries = max_retries
        self.timeout = timeout
        self.max_transcript_chars = max_transcript_chars
        self.num_predict = num_predict
        self.max_output_tokens = max_output_tokens

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
        Large participant lists (> _MAX_PARTICIPANTS_PER_CHUNK) are routed through
        _generate_mom_participant_chunked to keep each LLM response within the
        num_predict token cap.  Large transcripts are routed through
        _generate_mom_chunked so no content is ever discarded.
        """
        # Route large participant lists through participant-level chunking —
        # each group gets the full transcript but only outputs its participants,
        # keeping response tokens well under the num_predict cap.
        if len(participants) > _MAX_PARTICIPANTS_PER_CHUNK:
            return self._generate_mom_participant_chunked(
                transcript_text, participants, meeting_date, meeting_title, attendance
            )

        # Route long transcripts through transcript-level chunking — no content lost
        if len(transcript_text) > self.max_transcript_chars:
            return self._generate_mom_chunked(
                transcript_text, participants, meeting_date, meeting_title, attendance
            )

        attendance_context = self._format_attendance_context(attendance)
        truncated_transcript = self._truncate_transcript(transcript_text, self.max_transcript_chars)
        prompt = MOM_PROMPT_TEMPLATE.format(
            transcript=truncated_transcript,
            participants=", ".join(participants) if participants else "Not identified",
            meeting_date=meeting_date or "Not specified",
            meeting_title=meeting_title or "Team Meeting",
            attendance_context=attendance_context,
        )

        # ── Pre-request diagnostics ───────────────────────────
        effective_predict = min(self.num_predict, self.max_output_tokens)
        if effective_predict != self.num_predict:
            logger.warning(
                "[LLM] num_predict (%d) exceeds max_output_tokens ceiling (%d) — "
                "clamped to %d. Adjust 'num_predict' in settings.yaml.",
                self.num_predict, self.max_output_tokens, effective_predict,
            )
        logger.info(
            "[LLM REQUEST] model=%s | transcript=%d chars | prompt=%d chars | "
            "num_predict=%d | max_output_tokens=%d | effective_num_predict=%d | temperature=%.2f",
            self.model,
            len(transcript_text),
            len(prompt),
            self.num_predict,
            self.max_output_tokens,
            effective_predict,
            self.temperature,
        )

        for attempt in range(1, self.max_retries + 1):
            logger.info("LLM attempt %d/%d", attempt, self.max_retries)
            try:
                raw = self._call_ollama(prompt, num_predict_override=effective_predict)
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

        logger.error("All LLM attempts failed — pipeline will be halted.")
        raise LLMGenerationError(
            f"All {self.max_retries} LLM attempt(s) failed — "
            "Teams and email delivery halted. "
            "Check the 'num_predict' setting and Ollama server logs."
        )

    # ── Long-transcript chunked processing ────────────────────

    def _generate_mom_chunked(
        self,
        transcript_text: str,
        participants: List[str],
        meeting_date: str,
        meeting_title: str,
        attendance: Optional[Dict],
    ) -> Dict:
        """
        Process a transcript that is too large for a single LLM pass.
        Splits into overlapping chunks, extracts a partial MOM from each,
        then merges them into one complete MOM.  No transcript content is
        discarded.
        """
        chunks = self._split_into_chunks(transcript_text)
        n = len(chunks)
        logger.info(
            "Transcript (%d chars) exceeds single-pass limit (%d chars). "
            "Processing %d overlapping segments — no content will be discarded.",
            len(transcript_text), self.max_transcript_chars, n,
        )

        attendance_context = self._format_attendance_context(attendance)
        effective_predict = min(self.num_predict, self.max_output_tokens)
        partial_moms: List[Dict] = []
        failed_chunks: List[int] = []
        overlap = 3000

        for i, chunk in enumerate(chunks):
            chunk_start = i * (self.max_transcript_chars - overlap)
            logger.info(
                "Chunk %d/%d: %d chars (transcript offset ~%d)",
                i + 1, n, len(chunk), chunk_start,
            )

            annotated = (
                f"[SEGMENT {i + 1} OF {n} — extract all items from this segment only]\n\n"
                + chunk
            )
            prompt = MOM_PROMPT_TEMPLATE.format(
                transcript=annotated,
                participants=", ".join(participants) if participants else "Not identified",
                meeting_date=meeting_date or "Not specified",
                meeting_title=meeting_title or "Team Meeting",
                attendance_context=attendance_context,
            )

            chunk_result: Optional[Dict] = None
            for attempt in range(1, self.max_retries + 1):
                try:
                    raw = self._call_ollama(prompt, num_predict_override=effective_predict)
                    parsed = self._extract_json(raw)
                    chunk_result = self._validate_and_repair(
                        parsed, participants, meeting_title, meeting_date
                    )
                    logger.info("Chunk %d/%d succeeded on attempt %d.", i + 1, n, attempt)
                    break
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Chunk %d/%d JSON error attempt %d: %s", i + 1, n, attempt, exc
                    )
                except requests.exceptions.Timeout:
                    logger.warning("Chunk %d/%d timeout attempt %d.", i + 1, n, attempt)
                except Exception as exc:
                    logger.error(
                        "Chunk %d/%d error attempt %d: %s", i + 1, n, attempt, exc
                    )
                if attempt < self.max_retries:
                    time.sleep(5 * attempt)

            if chunk_result is None:
                logger.error(
                    "Chunk %d/%d failed all %d retries — "
                    "this segment will be missing from the MOM.",
                    i + 1, n, self.max_retries,
                )
                failed_chunks.append(i + 1)
            else:
                partial_moms.append(chunk_result)

        if not partial_moms:
            raise LLMGenerationError(
                f"All {n} transcript chunks failed extraction after "
                f"{self.max_retries} retries each."
            )

        merged = self._merge_chunk_moms(partial_moms, meeting_date, meeting_title)

        if failed_chunks:
            note = (
                f"WARNING: Segment(s) {failed_chunks} of {n} failed LLM extraction "
                "and are missing from this MOM. Review the transcript manually."
            )
            existing_reason = merged.get("status_reason", "").strip()
            merged["status_reason"] = (existing_reason + " | " + note) if existing_reason else note
            merged["overall_status"] = "HAS_ISSUES"
            logger.warning(note)

        logger.info(
            "Chunked extraction complete: %d/%d chunks succeeded | "
            "%d participants | %d action items | %d discussion points",
            len(partial_moms), n,
            len(merged.get("participants", [])),
            sum(len(p.get("action_items", [])) for p in merged.get("participants", [])),
            len(merged.get("discussion_points", [])),
        )
        return merged

    def _split_into_chunks(self, text: str, overlap: int = 3000) -> List[str]:
        """
        Split text into chunks of at most max_transcript_chars with
        'overlap' characters of shared content between adjacent chunks.
        The overlap prevents content that spans a boundary from being missed.
        """
        step = self.max_transcript_chars - overlap
        chunks: List[str] = []
        start = 0
        while start < len(text):
            chunks.append(text[start : start + self.max_transcript_chars])
            if start + self.max_transcript_chars >= len(text):
                break
            start += step
        return chunks

    def _merge_chunk_moms(
        self,
        partials: List[Dict],
        meeting_date: str,
        meeting_title: str,
    ) -> Dict:
        """
        Merge partial MOMs from transcript chunks into one complete MOM.
          - Scalar metadata : worst-case overall_status; first-chunk title/date
          - List fields     : union with case-insensitive deduplication
          - Participants    : merged by name; each per-person list is unioned
          - team_summary    : concatenated (each chunk covers different content)
          - follow_up_date  : last non-null across chunks
        """
        if len(partials) == 1:
            return partials[0]

        priority = {"MEETING_CANCELLED": 2, "HAS_ISSUES": 1, "ALL_CLEAR": 0}

        merged: Dict = {
            "meeting_title": meeting_title or partials[0].get("meeting_title", "Team Meeting"),
            "meeting_date": meeting_date or partials[0].get("meeting_date", ""),
            "overall_status": max(
                (p.get("overall_status", "ALL_CLEAR") for p in partials),
                key=lambda s: priority.get(s, 0),
            ),
            "status_reason": " | ".join(
                r for p in partials if (r := p.get("status_reason", "").strip())
            ),
            "team_summary": " ".join(
                s for p in partials if (s := p.get("team_summary", "").strip())
            ),
            "discussion_points": [],
            "key_decisions": [],
            "follow_up_date": next(
                (p["follow_up_date"] for p in reversed(partials) if p.get("follow_up_date")),
                None,
            ),
            "participants": [],
        }

        # Union list fields with deduplication
        for field in ("discussion_points", "key_decisions"):
            seen: set = set()
            for p in partials:
                for item in p.get(field, []):
                    key = str(item).strip().lower()[:120]
                    if key not in seen:
                        seen.add(key)
                        merged[field].append(item)

        # Merge participants by name
        by_name: Dict[str, Dict] = {}
        for p in partials:
            for participant in p.get("participants", []):
                name = participant.get("name", "").strip()
                if not name:
                    continue
                if name not in by_name:
                    by_name[name] = {
                        "name": name,
                        "yesterday": [],
                        "today": [],
                        "blockers": [],
                        "action_items": [],
                        "progress_summary": participant.get("progress_summary", ""),
                    }
                entry = by_name[name]
                for field in ("yesterday", "today", "blockers", "action_items"):
                    existing_keys = {str(i).strip().lower() for i in entry[field]}
                    for item in participant.get(field, []):
                        if str(item).strip().lower() not in existing_keys:
                            entry[field].append(item)
                            existing_keys.add(str(item).strip().lower())
                # Keep first non-trivial progress_summary
                if not entry["progress_summary"] and participant.get("progress_summary"):
                    entry["progress_summary"] = participant["progress_summary"]

        merged["participants"] = list(by_name.values())
        return merged

    # ── Participant-level chunked processing ──────────────────

    def _generate_mom_participant_chunked(
        self,
        transcript_text: str,
        participants: List[str],
        meeting_date: str,
        meeting_title: str,
        attendance: Optional[Dict],
    ) -> Dict:
        """
        Process a meeting with many participants by splitting them into groups
        of _MAX_PARTICIPANTS_PER_CHUNK.  Each group receives the same full
        transcript but only outputs data for its participants, keeping each
        LLM response within the num_predict token cap.
        """
        groups = [
            participants[i : i + _MAX_PARTICIPANTS_PER_CHUNK]
            for i in range(0, len(participants), _MAX_PARTICIPANTS_PER_CHUNK)
        ]
        n = len(groups)
        logger.info(
            "Meeting has %d participants — splitting into %d group(s) of up to %d each.",
            len(participants), n, _MAX_PARTICIPANTS_PER_CHUNK,
        )

        effective_predict = min(self.num_predict, self.max_output_tokens)
        truncated = self._truncate_transcript(transcript_text, self.max_transcript_chars)
        partial_moms: List[Dict] = []
        failed_groups: List[int] = []

        for i, group in enumerate(groups):
            logger.info(
                "[LLM REQUEST] Participant group %d/%d | model=%s | participants=%s | "
                "transcript=%d chars | num_predict=%d | temperature=%.2f",
                i + 1, n, self.model, ", ".join(group),
                len(transcript_text), effective_predict, self.temperature,
            )
            group_attendance = self._filter_attendance_for_group(attendance, group)
            prompt = MOM_PROMPT_TEMPLATE.format(
                transcript=truncated,
                participants=", ".join(group),
                meeting_date=meeting_date or "Not specified",
                meeting_title=meeting_title or "Team Meeting",
                attendance_context=self._format_attendance_context(group_attendance),
            )

            group_result: Optional[Dict] = None
            for attempt in range(1, self.max_retries + 1):
                try:
                    raw = self._call_ollama(prompt, num_predict_override=effective_predict)
                    parsed = self._extract_json(raw)
                    group_result = self._validate_and_repair(
                        parsed, group, meeting_title, meeting_date
                    )
                    logger.info("Group %d/%d succeeded on attempt %d.", i + 1, n, attempt)
                    break
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Group %d/%d JSON error attempt %d: %s", i + 1, n, attempt, exc
                    )
                except requests.exceptions.Timeout:
                    logger.warning("Group %d/%d timeout attempt %d.", i + 1, n, attempt)
                except Exception as exc:
                    logger.error(
                        "Group %d/%d error attempt %d: %s", i + 1, n, attempt, exc
                    )
                if attempt < self.max_retries:
                    time.sleep(5 * attempt)

            if group_result is None:
                logger.error(
                    "Group %d/%d failed all %d retries — participants %s missing from MOM.",
                    i + 1, n, self.max_retries, group,
                )
                failed_groups.append(i + 1)
            else:
                partial_moms.append(group_result)

        if not partial_moms:
            raise LLMGenerationError(
                f"All {n} participant group(s) failed extraction after "
                f"{self.max_retries} retries each."
            )

        merged = self._merge_chunk_moms(partial_moms, meeting_date, meeting_title)

        if failed_groups:
            note = (
                f"WARNING: Participant group(s) {failed_groups} of {n} failed LLM extraction "
                "and are missing from this MOM. Review the transcript manually."
            )
            existing_reason = merged.get("status_reason", "").strip()
            merged["status_reason"] = (existing_reason + " | " + note) if existing_reason else note
            merged["overall_status"] = "HAS_ISSUES"
            logger.warning(note)

        logger.info(
            "Participant-chunked extraction complete: %d/%d group(s) succeeded | "
            "%d participants | %d action items | %d discussion points",
            len(partial_moms), n,
            len(merged.get("participants", [])),
            sum(len(p.get("action_items", [])) for p in merged.get("participants", [])),
            len(merged.get("discussion_points", [])),
        )
        return merged

    def _filter_attendance_for_group(
        self, attendance: Optional[Dict], group: List[str]
    ) -> Optional[Dict]:
        """Return an attendance dict containing only members present in group."""
        if not attendance:
            return attendance
        group_lower = {name.lower() for name in group}
        filtered: Dict = {}
        for key, value in attendance.items():
            if isinstance(value, list):
                filtered[key] = [name for name in value if name.lower() in group_lower]
            else:
                filtered[key] = value
        return filtered

    # ── Ollama API ────────────────────────────────────────────

    def _call_ollama(self, prompt: str, num_predict_override: int = None) -> str:
        effective_predict = num_predict_override if num_predict_override is not None else min(self.num_predict, self.max_output_tokens)
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
                "num_predict": effective_predict,
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
                effective_predict,
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
        data.setdefault("discussion_points", [])
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
            "discussion_points": [],
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
