# AI Meeting Intelligence System

> **Local-first, enterprise-grade, open-source** — Automatically processes Microsoft Teams recordings, generates structured Minutes of Meeting per person, posts interactive Adaptive Cards to Teams, and sends formatted HTML emails. Includes optional **morale signal detection**, **draft approval gating**, and **personal weekly productivity digests**. No cloud dependency. No data leaves your machine.

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
9. [Optional Features](#optional-features)
10. [Real-World Edge Cases](#real-world-edge-cases)
11. [Deployment](#deployment)
12. [Running Tests](#running-tests)
13. [Tech Stack](#tech-stack)

---

## What It Does

```
Teams Meeting Recording                   Output
──────────────────────     ─────────────────────────────────────────────────
/ .vtt /   →               Per-person structured MOM:
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
                        →   [Optional] Manager email: Confidential morale report
                        →   [Optional] Organizer email: Draft MOM for review & approval
                        →   [Optional] Personal digest: Individual weekly summary email
```

---

## Architecture

```
OneDrive Sync Folder
       │  new /.vtt/
       ▼
  [watcher.py]  ──5-min delay──→  [parser.py]
  (watchdog)                       │   .vtt  → webvtt
                                   ▼
                             [llm_engine.py]
                              Ollama local API
                              gpt-oss:120b-cloud
                              temperature=0.1
                                   │  structured MOM JSON
                                   ├─────────▼ (Feature 8, optional)
                                   │          [analyze_sentiment()]
                                   │           → Manager morale email
                                   │
                                   ├─────────▼ (Feature 9, optional)
                                   │          [approval_server.py]
                                   │           → Draft email → Organizer
                                   │           → Wait for approve/reject
                                   │
                         ┌─────────┼─────────┐
                         ▼         ▼          ▼
                   [database.py] [teams_   [emailer.py]
                    SQLite       notifier]  SMTP + Jinja2
                                 Adaptive   HTML Template
                                 Card

  Weekly digest (Feature 10, manual / Task Scheduler):
  --weekly-digest → [digest.py] → SQLite query → LLM narrative
                               → Individual email per participant
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

# Retry failed files
python src\main.py --retry-failed

# Send weekly digest emails to all participants (Feature 10)
python src\main.py --weekly-digest

# Weekly digest for a custom date range (last 14 days)
python src\main.py --weekly-digest --digest-days 14
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
│   ├── parser.py                ← Transcript extraction (.vtt)
│   ├── llm_engine.py            ← Ollama API + JSON enforcement + sentiment
│   ├── teams_notifier.py        ← Adaptive Card builder + webhook sender
│   ├── emailer.py               ← SMTP + Jinja2 email service
│   ├── approval_server.py       ← [Feature 9] Localhost approval callback server
│   ├── digest.py                ← [Feature 10] Weekly digest orchestrator
│   └── attendance_parser.py     ← Teams CSV attendance parser
│
├── config/
│   ├── settings.yaml            ← Non-secret configuration
│   └── .env.example             ← Template for secrets
│
├── templates/
│   ├── email_template.html          ← Rich HTML team MOM email
│   ├── email_template_plain.txt     ← Plain-text fallback
│   ├── email_template_sentiment.html ← [Feature 8] Manager morale report
│   ├── email_draft_approval.html    ← [Feature 9] Draft approval email
│   ├── email_digest.html            ← [Feature 10] Personal weekly digest
│   └── adaptive_card_sample.json    ← Standalone card example (test in Designer)
│
├── docs/
│   ├── architecture.md          ← Full system design
│   ├── prompt_engineering.md    ← LLM prompt strategy guide
│   └── deployment.md            ← Windows Task Scheduler / NSSM guide
│
├── tests/
│   ├── test_parser.py           ← Parser unit tests
│   ├── test_llm_engine.py       ← LLM engine tests (no Ollama needed)
│   ├── test_teams_notifier.py   ← Card structure tests
│   ├── test_sentiment.py        ← [Feature 8] Sentiment analysis tests
│   ├── test_approval_server.py  ← [Feature 9] Approval server tests
│   └── test_digest.py           ← [Feature 10] Weekly digest tests
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
| `sentiment.enabled` | `false` | Enable morale signal detection (Feature 8) |
| `sentiment.manager_email` | `""` | Manager email to receive confidential morale reports |
| `approval.enabled` | `false` | Enable draft MOM approval gate (Feature 9) |
| `approval.organizer_email` | `""` | Organizer email to receive draft MOM for review |
| `approval.callback_port` | `8765` | Localhost port for approve/reject callbacks |
| `approval.timeout_minutes` | `30` | Minutes to wait before auto-action |
| `approval.auto_approve` | `true` | Auto-approve (`true`) or auto-reject (`false`) on timeout |
| `digest.enabled` | `false` | Enable weekly digest emails (Feature 10) |
| `digest.days_back` | `7` | Default date range for weekly digest |
| `digest.participant_emails` | `{}` | Map of `"Full Name": "email@company.com"` for digest recipients |

### Environment Variables (secrets only)

| Variable | Description |
|----------|-------------|
| `TEAMS_WEBHOOK_URL` | Teams Incoming Webhook URL |
| `EMAIL_USERNAME` | SMTP login email address |
| `EMAIL_PASSWORD` | SMTP app password |
| `WATCH_PATH` | Override watch path |
| `OLLAMA_BASE_URL` | Override Ollama URL |
| `SENTIMENT_MANAGER_EMAIL` | Override `sentiment.manager_email` from env |
| `APPROVAL_ORGANIZER_EMAIL` | Override `approval.organizer_email` from env |

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

**Additional analysis methods (Features 8 & 10):**
- `analyze_sentiment()` — Extracts tone and morale signals per participant; returns structured JSON with flags and confidence scores; all-neutral fallback if LLM fails
- `generate_digest_narrative()` — Produces a short, personalised narrative paragraph summarising a participant's week; used in weekly digest emails

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

**Email types sent:**
- `send_mom_email()` — Team MOM to all recipients (always active)
- `send_sentiment_email()` — Confidential morale report to manager only (Feature 8)
- `send_draft_approval_email()` — Draft MOM with approve/reject links to organizer (Feature 9)
- `send_digest_email()` — Personal weekly summary to individual participant (Feature 10)

### `database.py` — SQLite Storage

Stores every processed meeting:
- Raw transcript text
- Full MOM JSON
- Processing status (`pending`, `processing`, `completed`, `failed`)
- Notification delivery status (Teams + email)
- `sentiment_json` — stored sentiment analysis result per meeting (Feature 8)
- `approval_token`, `approval_status` — draft approval workflow state (Feature 9)

`INSERT OR IGNORE` ensures duplicate files are never processed twice.

**New query methods:**
- `update_sentiment()`, `set_awaiting_approval()`, `get_pending_approval_by_token()`, `set_approval_result()`, `get_meetings_by_date_range()`

### `approval_server.py` — Draft Approval Callback Server *(Feature 9)*

A minimal HTTP server bound strictly to `127.0.0.1` (never exposed externally).  
Runs in a background daemon thread, started once at application startup.

- **Routes:** `GET /approve?token=XXX`, `GET /reject?token=XXX`, `GET /health`
- **Tokens:** `secrets.token_urlsafe(32)` — cryptographically random, single-use
- **Thread safety:** `threading.Lock()` on all shared state
- `wait_for_decision(token, timeout_seconds, auto_approve)` blocks the pipeline until organizer responds or timeout fires
- Returns `"approved"`, `"rejected"`, or `"auto_approved"` / `"auto_rejected"` depending on the `auto_approve` setting

### `digest.py` — Weekly Digest Orchestrator *(Feature 10)*

Orchestrates the end-of-week summary workflow when invoked via `--weekly-digest`.

- Queries SQLite for all meetings in the configured date range
- Builds per-participant aggregated data: attended/absent counts, action items, blockers
- Detects **recurring blockers** (same blocker text appears in ≥2 separate meetings)
- Calls `llm_engine.generate_digest_narrative()` for a personalised AI paragraph
- Sends individual `send_digest_email()` to each person listed in `digest.participant_emails`
- Returns a `{name: success_bool}` result dictionary for logging

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

## Optional Features

All three features below are **disabled by default** (`enabled: false`). Zero existing behaviour is changed when they are off. Enable only what you need.

---

### Feature 8 — Sentiment & Morale Detection

**What it does:**  
After each meeting is processed, the LLM analyses the transcript for morale signals per participant. It assigns a tone (`confident`, `neutral`, `uncertain`, `frustrated`, `disengaged`) and a confidence score, and flags individuals showing concerning patterns. A confidential HTML report is emailed **only to the manager** — never to the team.

**Enable:**
```yaml
# config/settings.yaml
sentiment:
  enabled: true
  manager_email: "manager@yourcompany.com"
```

Or via environment variable:
```
SENTIMENT_MANAGER_EMAIL=manager@yourcompany.com
```

**Output:**  
`templates/email_template_sentiment.html` — orange gradient header, flagged member table, full team tone table. Subject line marked `[CONFIDENTIAL]`.

**Failure behaviour:**  
If the LLM cannot produce a valid sentiment result after retries, an all-neutral fallback is used and the pipeline continues normally. No meeting processing is blocked.

---

### Feature 9 — Draft MOM Approval Gate

**What it does:**  
Before the MOM email is sent to the team, the meeting organizer receives a draft preview email with two one-click buttons: **Approve** and **Reject**. The pipeline pauses and waits for the organizer's response. If no response arrives within the timeout, the system auto-approves or auto-rejects based on your `auto_approve` setting.

**Enable:**
```yaml
# config/settings.yaml
approval:
  enabled: true
  organizer_email: "organizer@yourcompany.com"
  callback_port: 8765       # localhost-only; never exposed externally
  timeout_minutes: 30
  auto_approve: true        # set false to auto-reject on timeout
```

Or via environment variable:
```
APPROVAL_ORGANIZER_EMAIL=organizer@yourcompany.com
```

**Security:**  
The callback server binds strictly to `127.0.0.1`. Approval tokens are `secrets.token_urlsafe(32)` (cryptographically random, 43 chars). Tokens are single-use and expire immediately upon use.

**Output:**  
`templates/email_draft_approval.html` — amber draft banner, full MOM preview, large green/red CTA buttons, localhost constraint note in footer.

---

### Feature 10 — Personal Weekly Digest

**What it does:**  
Run manually (or via Task Scheduler) to send each team member a personalised weekly productivity summary. Each person receives their own email showing: meetings attended/absent, all their action items, all blockers raised, **recurring blockers** (items that appeared in 2+ separate meetings), and an AI-generated narrative paragraph summarising their week.

**Enable:**
```yaml
# config/settings.yaml
digest:
  enabled: true
  days_back: 7              # date range to aggregate
  participant_emails:
    "Alice Johnson": "alice@yourcompany.com"
    "Bob Smith": "bob@yourcompany.com"
```

**Run:**
```powershell
# Last 7 days (uses digest.days_back from settings)
python src\main.py --weekly-digest

# Custom range
python src\main.py --weekly-digest --digest-days 14
```

**Output:**  
`templates/email_digest.html` — blue gradient header, stats row (attended / absent / action items / blockers), AI narrative block, recurring blockers section (orange), action items list, per-meeting breakdown table.

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
| Partial meeting (late join) | Known participants list fills gaps || Sentiment LLM fails | All-neutral fallback returned; pipeline continues unaffected |
| Approval gate not responded to | Auto-approved or auto-rejected based on `auto_approve` setting after timeout |
| Approval server port conflict | Startup error logged; approval feature gracefully disabled |
| No meetings in digest date range | Warning logged; returns empty dict; no emails sent |
| Participant absent from all meetings | Digest email shows 0 attended, no action items, still sent |
| Participant missing from `mom_json` | Counted as attended (silent); no action items collected |
| Recurring blocker text varies slightly | Case-insensitive exact-match; minor wording differences not merged |
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

# Run core module tests
pytest tests/test_llm_engine.py -v
pytest tests/test_teams_notifier.py -v
pytest tests/test_parser.py -v

# Run optional feature tests
pytest tests/test_sentiment.py -v        # Feature 8: Sentiment analysis
pytest tests/test_approval_server.py -v  # Feature 9: Approval callback server
pytest tests/test_digest.py -v           # Feature 10: Weekly digest

# Run entire suite with coverage summary
pytest tests/ -v --tb=short
```

All LLM tests use mocked data — no Ollama connection required to run tests.  
All approval server tests use `localhost:0` (OS-assigned port) — no port conflicts.

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
- Approval callback server binds to **`127.0.0.1` only** — never `0.0.0.0`
- Approval tokens use **`secrets.token_urlsafe(32)`** — cryptographically random, single-use
- Morale reports sent **only to the manager** — never CC'd or forwarded to the team
- No external API calls except: `localhost:11434` (Ollama), your Teams webhook, your SMTP server

---

*AI Meeting Intelligence System — Built for enterprise, runs locally, respects your data.*
