# AI Meeting Intelligence System

> **Local-first, enterprise-grade, open-source** — Automatically processes Microsoft Teams recordings, generates structured Minutes of Meeting per person, posts interactive Adaptive Cards to Teams, and sends formatted HTML emails. No cloud dependency. No data leaves your machine.

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [Architecture](#architecture)
3. [Quick Start](#quick-start)
4. [Project Structure](#project-structure)
5. [Configuration Reference](#configuration-reference)
6. [How Each Module Works](#how-each-module-works)
7. [Teams Adaptive Card](#teams-adaptive-card)
8. [Prompt Engineering](#prompt-engineering)
9. [Real-World Edge Cases](#real-world-edge-cases)
10. [Deployment](#deployment)
11. [Running Tests](#running-tests)
12. [Tech Stack](#tech-stack)

---

## What It Does

```
Teams Meeting Recording                   Output
──────────────────────     ─────────────────────────────────────────────────
.mp4 / .vtt / .docx   →   Per-person structured MOM:
(synced to OneDrive)       {
                             "name": "Alice Johnson",
                             "yesterday": [...],
                             "today": [...],
                             "blockers": [...],
                             "action_items": [...],
                             "progress_summary": "..."
                           }
                        →   Teams channel: Interactive Adaptive Card
                              (click each person to expand their updates)
                        →   Email: Clean HTML to all team members
                        →   SQLite: Full audit trail
```

---

## Architecture

```
OneDrive Sync Folder
       │  new .mp4/.vtt/.docx
       ▼
  [watcher.py]  ──5-min delay──→  [parser.py]
  (watchdog)                       │   .vtt  → webvtt
                                   │   .docx → python-docx
                                   │   .mp4  → faster-whisper
                                   ▼
                             [llm_engine.py]
                              Ollama local API
                              gpt-oss:120b-cloud
                              temperature=0.1
                                   │  structured MOM JSON
                         ┌─────────┼──────────┐
                         ▼         ▼          ▼
                   [database.py] [teams_   [emailer.py]
                    SQLite       notifier]  SMTP + Jinja2
                                 Adaptive   HTML Template
                                 Card
```

Full architecture detail: [docs/architecture.md](docs/architecture.md)

---

## Quick Start

### Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.10+ | |
| Ollama | Latest | https://ollama.com |
| Model | gpt-oss:120b-cloud | `ollama pull gpt-oss:120b-cloud` |
| Disk space | ~70 GB | For the 120B model |
| RAM | 16 GB min, 32 GB+ recommended | For 120B model inference |

### 1. Install Dependencies

```powershell
cd "AI Meeting Automation Guide"
pip install -r requirements.txt
```

Or use the setup script:
```powershell
scripts\setup.bat
```

### 2. Configure

```powershell
# Copy the example config
copy config\.env.example config\.env

# Edit config\.env with your secrets
notepad config\.env
```

Edit `config/settings.yaml`:
```yaml
watcher:
  watch_path: "C:/Users/YourName/OneDrive - Company/Recordings"

email:
  recipients:
    - "team@yourcompany.com"
```

### 3. Start Ollama

```powershell
ollama serve
# In a new terminal:
ollama pull gpt-oss:120b-cloud
```

### 4. Run

```powershell
# Start background watcher
python src\main.py

# OR — process a single file immediately
python src\main.py --file "C:\path\to\recording.vtt"

# Check status
python src\main.py --status
```

---

## Project Structure

```
AI Meeting Automation Guide/
│
├── src/                         ← All application code
│   ├── main.py                  ← Entry point & CLI
│   ├── config.py                ← Settings loader (YAML + env vars)
│   ├── database.py              ← SQLite persistence
│   ├── watcher.py               ← File system monitor
│   ├── parser.py                ← Transcript extraction (.vtt/.docx/.mp4)
│   ├── llm_engine.py            ← Ollama API + JSON enforcement
│   ├── teams_notifier.py        ← Adaptive Card builder + webhook sender
│   └── emailer.py               ← SMTP + Jinja2 email service
│
├── config/
│   ├── settings.yaml            ← Non-secret configuration
│   └── .env.example             ← Template for secrets
│
├── templates/
│   ├── email_template.html      ← Rich HTML email
│   ├── email_template_plain.txt ← Plain-text fallback
│   └── adaptive_card_sample.json← Standalone card example (test in Designer)
│
├── docs/
│   ├── architecture.md          ← Full system design
│   ├── prompt_engineering.md    ← LLM prompt strategy guide
│   └── deployment.md            ← Windows Task Scheduler / NSSM guide
│
├── tests/
│   ├── test_parser.py           ← Parser unit tests
│   ├── test_llm_engine.py       ← LLM engine tests (no Ollama needed)
│   └── test_teams_notifier.py   ← Card structure tests
│
├── scripts/
│   ├── setup.bat                ← First-time setup
│   └── run.bat                  ← Start with env loading
│
├── data/                        ← Auto-created (meetings.db)
├── logs/                        ← Auto-created (meeting_system.log)
└── requirements.txt
```

---

## Configuration Reference

### `config/settings.yaml`

| Key | Default | Description |
|-----|---------|-------------|
| `watcher.watch_path` | `""` | Absolute path to recordings folder |
| `watcher.delay_seconds` | `300` | Seconds to wait after file appears |
| `ollama.model` | `gpt-oss:120b-cloud` | Ollama model name |
| `ollama.temperature` | `0.1` | LLM randomness (keep low) |
| `ollama.timeout` | `300` | Max seconds per LLM call |
| `ollama.max_retries` | `3` | Retry attempts on failure |
| `teams.enabled` | `true` | Enable Teams notifications |
| `email.smtp_host` | `smtp.office365.com` | SMTP server |
| `email.smtp_port` | `587` | SMTP port (STARTTLS) |
| `email.recipients` | `[]` | List of email addresses |

### Environment Variables (secrets only)

| Variable | Description |
|----------|-------------|
| `TEAMS_WEBHOOK_URL` | Teams Incoming Webhook URL |
| `EMAIL_USERNAME` | SMTP login email address |
| `EMAIL_PASSWORD` | SMTP app password |
| `WATCH_PATH` | Override watch path |
| `OLLAMA_BASE_URL` | Override Ollama URL |

---

## How Each Module Works

### `watcher.py` — File System Monitor

Uses `watchdog` to watch the OneDrive recordings folder.  
When a file appears (created or moved in), a **5-minute countdown timer** starts.  
This prevents processing a file while OneDrive is still syncing it.  
If the file changes again during the delay, the timer resets.  
Already-processed files are skipped via the database idempotency check.

### `parser.py` — Transcript Extraction

**Priority chain:**
1. `.vtt` → Most accurate (Teams native export, speaker-tagged)
2. `.docx` → Good (Word transcript, handles Teams export format)
3. `.mp4` → Slowest (CPU Whisper, no speaker IDs by default)

**Speaker extraction from VTT:**
```
<v Alice Johnson>Good morning.</v>  →  speaker="Alice Johnson", text="Good morning."
Alice Johnson: Good morning.        →  speaker="Alice Johnson", text="Good morning."
```

**Consecutive speaker merging** reduces LLM token count:
```
Alice: "First sentence."  ─┐
Alice: "Second sentence."  ─┘  →  Alice: "First sentence. Second sentence."
```

### `llm_engine.py` — Local LLM Integration

Calls Ollama's `/api/generate` API with:
- **System prompt:** Forces JSON-only output
- **User prompt:** Provides transcript, known participants, strict rules
- **Temperature 0.1:** Deterministic, factual output
- **Stop sequences:** Prevent post-JSON rambling

**Response handling pipeline:**
```
Raw text → strip fences → find { boundary → find matching } → repair JSON → json.loads()
```

**Validation:** Checks every required field exists. Adds missing participants with empty arrays. Never crashes — returns safe fallback MOM if all retries fail.

### `teams_notifier.py` — Adaptive Card Builder

Builds an Adaptive Card JSON payload with:
- **Header** with status badge (green/red)
- **Per-person toggle buttons** — clicking shows/hides that person's details
- **Hidden containers** (isVisible: false) for each person's full updates
- **"Expand All" button** at card level to show all at once

Uses `Action.ToggleVisibility` — no backend calls needed, fully client-side in Teams.

### `emailer.py` — SMTP Email Service

Renders Jinja2 templates (HTML + plain text) with the MOM data.  
Sends via `smtplib.SMTP` with STARTTLS.  
HTML template uses `autoescape=True` to prevent XSS.

### `database.py` — SQLite Storage

Stores every processed meeting:
- Raw transcript text
- Full MOM JSON
- Processing status (`pending`, `processing`, `completed`, `failed`)
- Notification delivery status (Teams + email)

`INSERT OR IGNORE` ensures duplicate files are never processed twice.

---

## Teams Adaptive Card

The card uses **`Action.ToggleVisibility`** — no server round-trip needed.

### Visual Layout

```
┌─────────────────────────────────────────────────────┐
│  📋 Daily Standup Report           ⚠️ HAS ISSUES    │
│  Sprint Planning • 2026-04-29                        │
├─────────────────────────────────────────────────────┤
│  ⚠️ 2 blockers reported                             │
├─────────────────────────────────────────────────────┤
│  🏢 Team Summary                                    │
│  Team made solid progress...                         │
├─────────────────────────────────────────────────────┤
│  👥 Team Members — click to expand                  │
│  ┌──────────┬──────────┬──────────┐                │
│  │🟢 Alice  │🔴 Bob   │🔴 Carol  │                │
│  └──────────┴──────────┴──────────┘                │
│  ┌──────────┐                                       │
│  │🟢 David  │                                       │
│  └──────────┘                                       │
├─────────────────────────────────────────────────────┤
│  [When Bob is clicked, this expands:]               │
│  📊 Bob Smith                                       │
│  Bob is blocked on vendor API credentials...         │
│  📅 Yesterday  › Set up payment SDK                 │
│  🎯 Today      › Waiting on credentials             │
│  ✅ Action Items › Follow up with vendor — by 2 PM  │
│  🚫 Blockers   › Waiting for Stripe sandbox API key │
├─────────────────────────────────────────────────────┤
│  [📋 Expand / Collapse All Members]                 │
└─────────────────────────────────────────────────────┘
```

**Test your card:** Paste `templates/adaptive_card_sample.json` into  
https://adaptivecards.io/designer/ to preview before sending.

---

## Prompt Engineering

See the full guide: [docs/prompt_engineering.md](docs/prompt_engineering.md)

### Key Techniques

| Technique | Why |
|-----------|-----|
| `temperature: 0.1` | Deterministic, factual output |
| System prompt forbids markdown | Prevents code fence wrapping |
| Rules numbered explicitly | Model follows ordered instructions |
| JSON skeleton in prompt | Model continues the pattern |
| "Do NOT infer" stated 3 ways | Redundancy reduces hallucination |
| Stop sequences | Stops rambling after `}` |
| Brace-depth JSON extraction | Handles nested objects correctly |
| `_repair_json()` | Fixes trailing commas silently |
| 3 retries + backoff | Recovers from occasional bad outputs |
| Safe fallback MOM | Never crashes the pipeline |

---

## Real-World Edge Cases

| Scenario | How It's Handled |
|----------|-----------------|
| OneDrive still syncing | 5-min delay timer; resets on file change |
| Same file processed twice | `INSERT OR IGNORE` + `is_processed()` |
| Speaker name variations | Known participants list in prompt for normalization |
| `[inaudible]` in transcript | Regex-cleaned before LLM processing |
| Very long meeting (2hr+) | Transcript truncated, keeping start + end |
| LLM returns bad JSON | 3 retries; fallback MOM on all failures |
| Participant not in transcript | Added to MOM with empty arrays |
| Teams webhook unreachable | Logged warning; email still sends |
| SMTP auth failure | Specific error + guidance message |
| .mp4 without speaker IDs | Whisper transcribes; LLM extracts without names |
| Partial meeting (late join) | Known participants list fills gaps |

---

## Deployment

Full guide: [docs/deployment.md](docs/deployment.md)

### Quick Options

```powershell
# Option A: Windows Task Scheduler
# Set up via Task Scheduler GUI — see deployment.md

# Option B: NSSM Windows Service
nssm install AIMeetingSystem python "src\main.py"
nssm start AIMeetingSystem

# Option C: Simple startup
scripts\run.bat   # Add to Startup folder
```

---

## Running Tests

```powershell
# Install test dependencies (included in requirements.txt)
pip install pytest pytest-mock

# Run all tests
pytest tests/ -v

# Run specific module tests
pytest tests/test_llm_engine.py -v
pytest tests/test_teams_notifier.py -v
pytest tests/test_parser.py -v
```

All LLM tests use mocked data — no Ollama connection required to run tests.

---

## Tech Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| Language | Python 3.10+ | Rich ecosystem, watchdog, SMTP built-in |
| File watching | watchdog 4.0 | Cross-platform, battle-tested |
| Audio transcription | faster-whisper | CPU-friendly, int8 quantization |
| VTT parsing | webvtt-py | Handles Teams VTT format |
| DOCX parsing | python-docx | Reads Teams Word exports |
| LLM | Ollama + gpt-oss:120b | Local inference, no cloud |
| Teams integration | Adaptive Cards + Incoming Webhook | No bot framework needed |
| Email | smtplib + Jinja2 | Zero dependencies on external services |
| Storage | SQLite | File-based, zero config, fast |
| Config | PyYAML | Human-readable, env var override |

---

## Security

- All secrets live in **environment variables** — never in YAML
- HTML email templates use **Jinja2 autoescape** (XSS protection)  
- All SQL uses **parameterized queries** (SQL injection protection)
- SMTP uses **STARTTLS** (encrypted in transit)
- No external API calls except: `localhost:11434` (Ollama), your Teams webhook, your SMTP server

---

*AI Meeting Intelligence System — Built for enterprise, runs locally, respects your data.*
