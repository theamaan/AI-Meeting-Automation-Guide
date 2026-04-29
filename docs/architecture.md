# Architecture — AI Meeting Intelligence System

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    AI MEETING INTELLIGENCE SYSTEM                           │
│                         (Local-First Architecture)                          │
└─────────────────────────────────────────────────────────────────────────────┘

  OneDrive Sync          File System            Processing Engine
  ─────────────          ───────────            ─────────────────
  Teams Meeting  ──→  C:/Users/.../           watcher.py
  Recording            Recordings/             (watchdog)
  (.mp4/.vtt/.docx)       │                       │
                          │  [new file event]      │
                          └──────────────────→  5-min delay
                                                   │
                                                   ▼
                                             parser.py
                                         ┌───────────────┐
                                         │  .vtt  → webvtt│
                                         │  .docx → docx  │
                                         │  .mp4  → whisper│
                                         └───────┬───────┘
                                                 │ ParsedTranscript
                                                 ▼
                                          llm_engine.py
                                         ┌───────────────┐
                                         │ Ollama API    │
                                         │ gpt-oss:120b  │
                                         │ localhost:11434│
                                         └───────┬───────┘
                                                 │ MOM JSON (per person)
                                    ┌────────────┼────────────┐
                                    ▼            ▼            ▼
                             database.py  teams_notifier  emailer.py
                             (SQLite)      .py (Webhook)   (SMTP)
                                │              │               │
                                │         Adaptive Card    HTML Email
                                │         Teams Channel    Recipients
                                │
                          data/meetings.db
                          (transcript + MOM
                           + status + timestamps)
```

---

## Component Responsibilities

| Component | File | Responsibility |
|-----------|------|----------------|
| File Watcher | `src/watcher.py` | Monitor folder, debounce with timer, dispatch pipeline |
| Transcript Parser | `src/parser.py` | Extract speaker-segmented text from .vtt/.docx/.mp4 |
| LLM Engine | `src/llm_engine.py` | Call Ollama, enforce JSON, retry on failure |
| Teams Notifier | `src/teams_notifier.py` | Build Adaptive Card, POST to webhook |
| Email Service | `src/emailer.py` | Render Jinja2 templates, send via SMTP |
| Database | `src/database.py` | SQLite persistence, idempotency guard |
| Config | `src/config.py` | Load YAML + env var overlay |
| Orchestrator | `src/main.py` | Wire all components, CLI, graceful shutdown |

---

## Data Flow (Step by Step)

```
1.  Teams meeting ends
2.  Recording syncs to OneDrive folder via desktop client
3.  watchdog detects file creation or move event
4.  5-minute timer starts (waiting for sync to finish)
5.  Timer fires → _fire() checks file still exists
6.  Database checked: is this file already processed?  → if yes, skip
7.  parser.py selects correct handler:
      .vtt  → webvtt.read() → speaker extraction → TranscriptSegment[]
      .docx → python-docx → paragraph scan → TranscriptSegment[]
      .mp4  → faster-whisper → VAD + transcription → TranscriptSegment[]
8.  Consecutive same-speaker segments merged (reduce LLM token count)
9.  Speaker list extracted (unique names)
10. Filename parsed for meeting date and title
11. llm_engine calls Ollama /api/generate with structured prompt
12. Response cleaned of markdown fences, JSON extracted
13. JSON schema validated; missing participants inserted with empty arrays
14. Up to 3 retries with exponential backoff on parse failures
15. mom_data dict saved to SQLite
16. teams_notifier builds Adaptive Card JSON, POSTs to webhook
17. emailer renders Jinja2 templates, sends via SMTP STARTTLS
18. DB updated with notification status
```

---

## Database Schema

```sql
CREATE TABLE meetings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT    UNIQUE NOT NULL,   -- Full path, prevents duplicate processing
    file_name       TEXT    NOT NULL,
    file_type       TEXT    NOT NULL,          -- .vtt | .docx | .mp4
    processed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    transcript      TEXT,                      -- Raw speaker-prefixed text
    mom_json        TEXT,                      -- Full MOM JSON (stringified)
    meeting_date    TEXT,
    meeting_title   TEXT,
    status          TEXT    DEFAULT 'pending', -- pending | processing | completed | failed
    error_message   TEXT,
    teams_notified  INTEGER DEFAULT 0,
    email_sent      INTEGER DEFAULT 0
);
```

---

## Adaptive Card Layout (Visual)

```
┌──────────────────────────────────────────────────────────────┐
│  📋 Daily Standup Report                    ⚠️ HAS ISSUES    │
│  Sprint Planning • 2026-04-29                                 │
├──────────────────────────────────────────────────────────────┤
│  ⚠️  2 blockers reported — requires immediate attention      │
├──────────────────────────────────────────────────────────────┤
│  🏢 Team Summary                                             │
│  The team made solid progress on auth module...              │
├──────────────────────────────────────────────────────────────┤
│  👥 Team Members — click to expand                           │
│  ┌────────────┬────────────┬────────────┐                   │
│  │🟢 Alice J. │🔴 Bob S.  │🔴 Carol L. │  ← clickable      │
│  └────────────┴────────────┴────────────┘                   │
│  ┌────────────┐                                              │
│  │🟢 David K. │                                              │
│  └────────────┘                                              │
├──────────────────────────────────────────────────────────────┤
│  📊 Bob Smith                          [hidden by default]   │
│  Bob is blocked on vendor API credentials...                 │
│  📅 Yesterday  • Set up payment SDK scaffolding              │
│  🎯 Today      • Waiting on vendor API credentials           │
│  ✅ Action Items • Follow up with vendor by 2 PM             │
│  🚫 Blockers   • Waiting for Stripe sandbox API key          │
├──────────────────────────────────────────────────────────────┤
│  🔑 Key Decisions                                            │
│  • Demo moved to Monday                                      │
├──────────────────────────────────────────────────────────────┤
│           [📋 Expand / Collapse All Members]                 │
└──────────────────────────────────────────────────────────────┘
```

---

## Error Handling Strategy

| Failure Point | Recovery |
|---------------|----------|
| File disappears before processing | `_fire()` checks existence, logs warning, skips |
| VTT parse error | Exception propagates, `db.mark_failed()`, logged |
| Whisper import missing | RuntimeError with install instruction |
| Ollama unreachable | Startup health check exits; during run: 3 retries + backoff |
| LLM returns bad JSON | Regex extraction + repair; 3 retries; fallback MOM on all fail |
| LLM hallucinates structure | `_validate_and_repair()` fills missing fields |
| Teams webhook fails | Logged warning, does NOT block email |
| SMTP auth error | Specific error message with guidance |
| Duplicate file | `INSERT OR IGNORE` + `is_processed()` check |

---

## Security Considerations

- Credentials in **environment variables only** — never in YAML
- Jinja2 `autoescape=True` on HTML templates — prevents XSS in emails
- SQLite parameterized queries — prevents SQL injection
- No external API calls except: Ollama (localhost), Teams webhook (internal), SMTP
- Error messages stored ≤1000 chars (prevents log injection)
- STARTTLS enforced for email
