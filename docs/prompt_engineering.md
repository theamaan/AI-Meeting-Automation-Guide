# Prompt Engineering Guide
## Getting Reliable Structured JSON from Local LLMs

---

## The Core Challenge

Large local models (gpt-oss:120b, LLaMA 3) are powerful but unpredictable without
guardrails. They tend to:
- Add explanatory text before or after the JSON
- Use markdown code fences  ````json ... ` ` ` ``
- Invent data for missing participants ("hallucinate")
- Use trailing commas in JSON arrays
- Mix single and double quotes

This guide explains every technique used to combat these issues.

---

## The Two-Prompt Strategy

### System Prompt (injected as `system` field in Ollama API)

```
You are an expert enterprise meeting analyst.
Your ONLY job is to output valid JSON.
Do NOT output any explanation, markdown, code blocks, or extra text.
Start your response with { and end with }.
Never hallucinate. Only use information explicitly stated in the transcript.
```

**Why this works:**
- Sets a single, unmistakable role
- Explicitly forbids common failure modes by naming them
- The "Start with { end with }" instruction dramatically reduces preamble

### User Prompt (the actual task)

See `llm_engine.py` → `MOM_PROMPT_TEMPLATE`. Key design decisions:

---

## Prompt Design Principles

### 1. Give the JSON skeleton in the prompt

```
OUTPUT — return ONLY this JSON, nothing else:
{
  "meeting_title": "...",
  "participants": [
    {
      "name": "Full Name",
      "yesterday": ["item"],
      ...
    }
  ]
}
```

**Why:** Models are token predictors. When they see `{` in the expected output,
they continue generating JSON. Without the skeleton, they may describe the JSON
instead of producing it.

### 2. Number your rules explicitly

```
STRICT EXTRACTION RULES:
1. Extract per participant: yesterday, today, blockers...
2. If a participant did not speak, include them with empty arrays.
3. Do NOT infer. Do NOT fill in gaps. Use [] for missing data.
```

**Why:** Numbered rules are tokenized sequentially. The model "follows along"
and is less likely to skip constraints it has "read."

### 3. Repeat the key constraint in multiple ways

In the prompt you'll see these three variations of the same rule:
```
"Only use information explicitly stated in the transcript."
"Do NOT infer."
"Never hallucinate."
```

This redundancy is intentional. The model's attention mechanism activates on
repeated patterns — saying it once is not enough.

### 4. Provide the format string for double braces

In Python `.format()` calls, `{{` and `}}` are literal braces. In the prompt
template, the JSON skeleton uses `{{` to prevent Python from interpreting them
as format placeholders while still showing the model `{` in the prompt.

### 5. Low temperature (0.1)

```python
"temperature": 0.1
```

Temperature controls randomness. At 0.1:
- The model picks the highest-probability token almost every time
- Output is deterministic across runs
- Less creative, more factual — exactly what we want for MOM extraction

At 0.7 (default), the model might write poetry. At 0.1, it writes JSON.

### 6. Stop sequences

```python
"stop": ["\n```", "```\n", "\n\nNote:", "\n\nExplanation:"]
```

These tell the model to stop generating when it tries to add post-JSON commentary.
Especially important after closing `}` — models often add "I hope this helps!"

---

## JSON Extraction Pipeline

Even with perfect prompting, the model occasionally wraps output. The extraction
pipeline in `llm_engine._extract_json()` handles this:

```
Raw LLM output
     │
     ▼
Strip markdown fences (```json ... ```)
     │
     ▼
Find first { using text.find('{')
     │
     ▼
Find matching } using brace depth counter
(NOT rfind('}') — that breaks on JSON with nested objects)
     │
     ▼
Repair common mistakes:
  - Trailing commas:  ["item",]  →  ["item"]
     │
     ▼
json.loads()
     │
  ┌──┴──┐
  │     │
 OK   JSONDecodeError
  │        │
  ▼        ▼
Validate  Retry (up to 3x)
schema    with backoff
```

### Why brace-depth matching instead of `rfind('}')`?

```json
{
  "key": "value with } inside",
  "nested": { "a": 1 }
}
```

`rfind('}')` would find the last `}` correctly here, but if the model adds
text after the JSON:

```
{"key": "value"}

Note: I have generated the requested JSON above.
```

`rfind('}')` still works — but if the trailing text contains `}`:

```
{"key": "value"}

Note: Use {} to add more items.
```

Now `rfind('}')` finds the wrong position. Brace-depth matching is O(n) but
handles all these cases correctly.

---

## Handling Noisy Transcripts

Real-world transcripts are messy. Here's how each issue is handled:

### Multiple spellings of the same name

**Problem:** "Bob Smith", "bob", "Bob S.", "B. Smith" all refer to the same person.

**Current approach:** Pass known participant names in the prompt:
```
KNOWN PARTICIPANTS: Alice Johnson, Bob Smith, Carol Lee, David Khan
```
The LLM will normalize names to the provided list.

**Advanced approach (if needed):** Pre-process with fuzzy matching:
```python
from difflib import get_close_matches
normalized = get_close_matches(raw_name, known_names, n=1, cutoff=0.6)
```

### [inaudible] / [crosstalk] artifacts

Handled in `parser._clean_text()`:
```python
text = re.sub(r"\[.*?\]", "", text)   # Remove [inaudible], [crosstalk], [music]
```

### Filler words ("um", "uh", "you know")

The LLM naturally filters these when extracting structured items. The prompt
says "under 20 words per item" which forces compression.

### Background noise transcription (Whisper artifacts)

Whisper's `vad_filter=True` skips silence. Short noise bursts produce short
garbled segments which the LLM ignores since they don't form coherent sentences.

### Very long transcripts (context window overflow)

The `_truncate_transcript()` method keeps the first and last `max_chars/2`
characters:

```
[First 7,000 chars of transcript]
... 15,000 characters omitted ...
[Last 7,000 chars of transcript]
```

**Why keep the end?** Action items and decisions are typically stated at the end
of meetings ("So to summarize, Bob you'll..."). The end is more information-dense
for MOM purposes.

### Partial meetings (late joiners, early leavers)

The prompt instructs: "If a participant did not speak, include them with empty arrays."

This handles the case where someone's name is known (from the meeting invite) but
they joined late and aren't in the transcript.

### Single-speaker transcripts (Whisper without diarization)

When using Whisper on .mp4, all text is labelled "Transcribed" (no speaker IDs).
The LLM prompt still works — it will extract tasks and blockers from the raw text —
but won't be able to assign them to specific individuals accurately.

**Recommended fix:** Use pyannote.audio for speaker diarization:
```bash
pip install pyannote.audio
# Requires a HuggingFace token and model download
```

Then combine Whisper timestamps with pyannote speaker segments.

---

## Prompt Variations for Edge Cases

### When participants are unknown (no known list)

Change the prompt section to:
```
KNOWN PARTICIPANTS: Not identified — extract all speaker names from the transcript.
```

The LLM will try to identify speakers from "Name:" patterns or context.

### For very formal meetings (Board, Executive)

Add to the rules section:
```
4. TONE: Formal. Use professional language in progress_summary.
5. DECISIONS: Be especially thorough in key_decisions extraction.
```

### For technical stand-ups

Add:
```
4. For action_items, include the JIRA ticket number if mentioned (e.g., "Fix bug — PROJ-421 — by Thursday").
5. For blockers, include the dependency owner if mentioned.
```

---

## Testing Your Prompts

Use the manual file processing mode to test with a single transcript:

```bash
cd src
python main.py --file path/to/test.vtt
```

Then check the output:
```bash
python main.py --status
```

Or query SQLite directly:
```sql
SELECT json_pretty(mom_json) FROM meetings WHERE file_name = 'test.vtt';
```

---

## Common LLM Failure Patterns & Fixes

| Failure | Symptom | Fix |
|---------|---------|-----|
| Preamble text | `"Here is the JSON: {...}"` | `_extract_json()` finds `{` |
| Code fence wrap | ` ```json {...} ``` ` | Regex strips fences first |
| Trailing comma | `["item",]` | `_repair_json()` removes them |
| Wrong status | `"overall_status": "CLEAR"` | `_validate_and_repair()` normalizes to `ALL_CLEAR` |
| Missing participant | Person not in output | `_validate_and_repair()` adds with empty arrays |
| Lists as strings | `"yesterday": "did X"` | Validation converts to `["did X"]` |
| Too long items | 50-word action items | Prompt says "under 20 words" |
| Hallucinated items | Data not in transcript | Low temperature + "Do NOT infer" rule |
