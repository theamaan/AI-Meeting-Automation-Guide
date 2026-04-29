# Deployment Guide
## Running the AI Meeting Intelligence System in Production (Windows)

---

## Option A — Windows Task Scheduler (Recommended for Enterprise)

Run the system as a background service that auto-starts with Windows.

### Step 1: Create a Scheduled Task

1. Open **Task Scheduler** (search in Start Menu)
2. Click **"Create Task"** (not Basic Task)
3. Fill in:

**General Tab:**
- Name: `AI Meeting Intelligence System`
- Description: `Processes Teams meeting recordings and generates MOM`
- Select: `Run whether user is logged on or not`
- Select: `Run with highest privileges`
- Configure for: `Windows 10` or `Windows 11`

**Triggers Tab:**
- Click **New...**
- Begin the task: `At startup`
- Delay task for: `2 minutes` (let OneDrive sync initialize)

**Actions Tab:**
- Click **New...**
- Action: `Start a program`
- Program/script: `C:\Python311\python.exe` (your Python path)
- Add arguments: `src\main.py --config config\settings.yaml`
- Start in: `\\molina.mhc\mhroot\CorpIS\OM\Aman Ullah\Projects\AI Meeting Automation Guide`

**Conditions Tab:**
- Uncheck: `Stop the task if the computer switches to battery power`

**Settings Tab:**
- Check: `If the task fails, restart every: 1 minute, for up to 3 attempts`
- Check: `Run task as soon as possible after a scheduled start is missed`

### Step 2: Set Environment Variables

These must be set in **System Environment Variables** (not user-level) so the
scheduled task can access them:

```
TEAMS_WEBHOOK_URL = https://your-company.webhook.office.com/webhookb2/...
EMAIL_USERNAME    = ai-meeting@yourcompany.com
EMAIL_PASSWORD    = your_app_password
WATCH_PATH        = C:\Users\ServiceAccount\OneDrive - YourCompany\Recordings
```

**To set system environment variables:**
1. Right-click **This PC** → Properties
2. Advanced system settings → Environment Variables
3. Under **System variables** → New

---

## Option B — Windows Service with NSSM (More Robust)

NSSM (Non-Sucking Service Manager) wraps your Python script as a proper Windows service.

### Install NSSM

```powershell
# Download from https://nssm.cc/download
# Or via Chocolatey:
choco install nssm
```

### Register the Service

```powershell
# Run as Administrator
nssm install "AIMeetingSystem" "C:\Python311\python.exe" `
  "src\main.py --config config\settings.yaml"

nssm set "AIMeetingSystem" AppDirectory `
  "\\molina.mhc\mhroot\CorpIS\OM\Aman Ullah\Projects\AI Meeting Automation Guide"

nssm set "AIMeetingSystem" AppEnvironmentExtra `
  "TEAMS_WEBHOOK_URL=https://..." `
  "EMAIL_USERNAME=ai-meeting@company.com" `
  "EMAIL_PASSWORD=your_password"

nssm set "AIMeetingSystem" AppStdout `
  "logs\service_stdout.log"

nssm set "AIMeetingSystem" AppStderr `
  "logs\service_stderr.log"

nssm set "AIMeetingSystem" Start SERVICE_AUTO_START

nssm start "AIMeetingSystem"
```

### Service Management

```powershell
nssm status AIMeetingSystem    # Check status
nssm restart AIMeetingSystem   # Restart
nssm stop AIMeetingSystem      # Stop
nssm remove AIMeetingSystem    # Uninstall
```

---

## Option C — Simple Startup Script (Development / Testing)

Create a `.bat` file and add to Startup folder:

```batch
:: run.bat
@echo off
cd /d "\\molina.mhc\mhroot\CorpIS\OM\Aman Ullah\Projects\AI Meeting Automation Guide"

:: Load environment variables from .env file
for /f "tokens=1,2 delims==" %%a in (.env) do set %%a=%%b

:: Start Ollama first (if not running)
start "" ollama serve

:: Wait for Ollama to be ready
timeout /t 5 /nobreak > nul

:: Start the meeting system
python src\main.py --config config\settings.yaml
```

---

## Ollama Setup & Model Pull

```powershell
# Install Ollama (download from https://ollama.com)

# Pull the model (first-time only — requires internet)
ollama pull gpt-oss:120b-cloud

# Verify it's loaded
ollama list

# Test the model
ollama run gpt-oss:120b-cloud "Say hello in JSON format"

# Start the Ollama server (runs on localhost:11434)
ollama serve
```

**Corporate Firewall Note:**
- Ollama runs entirely local after model download
- Only the initial `ollama pull` requires internet access
- Model files stored at: `C:\Users\<username>\.ollama\models\`
- Estimated disk space: ~70GB for 120B model

---

## Directory Structure After Setup

```
AI Meeting Automation Guide/
├── src/
│   ├── main.py            ← Entry point
│   ├── watcher.py
│   ├── parser.py
│   ├── llm_engine.py
│   ├── teams_notifier.py
│   ├── emailer.py
│   ├── database.py
│   └── config.py
├── config/
│   ├── settings.yaml      ← Non-secret config
│   └── .env               ← SECRETS (never commit)
├── templates/
│   ├── email_template.html
│   ├── email_template_plain.txt
│   └── adaptive_card_sample.json
├── data/
│   └── meetings.db        ← Auto-created
├── logs/
│   └── meeting_system.log ← Auto-created
├── docs/
├── scripts/
│   ├── setup.bat
│   └── run.bat
└── requirements.txt
```

---

## Health Check & Monitoring

### Manual Status Check

```powershell
cd "\\molina.mhc\mhroot\CorpIS\OM\Aman Ullah\Projects\AI Meeting Automation Guide"
python src\main.py --status
```

Output:
```
────────────────────────────────────────────────────────────────────────────────
  File                                Status       Date         Teams   Email
────────────────────────────────────────────────────────────────────────────────
  Daily Standup-20260429.vtt          COMPLETED    2026-04-29   ✓       ✓
  Sprint Review-20260428.mp4          COMPLETED    2026-04-28   ✓       ✓
  Backlog Grooming-20260427.vtt       FAILED       2026-04-27   ✗       ✗
```

### Retry Failed Files

```powershell
python src\main.py --retry-failed
```

### Log Monitoring

```powershell
# Watch live log (PowerShell)
Get-Content logs\meeting_system.log -Wait -Tail 50

# Or
tail -f logs\meeting_system.log   # if Git Bash available
```

---

## Teams Webhook Setup (Step by Step)

1. Open **Microsoft Teams**
2. Navigate to the **channel** where you want MOM cards posted
3. Click **⋯ More options** next to the channel name
4. Select **Connectors**
5. Find **Incoming Webhook** → click **Configure**
6. Name it: `AI Meeting System`
7. Upload a logo (optional)
8. Click **Create**
9. **Copy the webhook URL** — this is a one-time display
10. Set it as environment variable: `TEAMS_WEBHOOK_URL=<copied URL>`

**Security:** The webhook URL is a shared secret. Anyone with this URL can
post to your channel. Treat it like a password.

---

## SMTP Configuration

### Office 365
```yaml
smtp_host: "smtp.office365.com"
smtp_port: 587
use_tls: true
```
Requires: Modern Authentication enabled OR App Password.

### Gmail
```yaml
smtp_host: "smtp.gmail.com"
smtp_port: 587
use_tls: true
```
Requires: 2FA enabled + App Password (account password won't work).
Create App Password at: myaccount.google.com/apppasswords

---

## Performance Benchmarks (Approximate)

| Step | .vtt file | .docx file | .mp4 (60 min) |
|------|-----------|------------|----------------|
| Parsing | < 1s | < 1s | 5–15 min (Whisper) |
| LLM Analysis | 2–8 min | 2–8 min | 2–8 min |
| Teams Send | < 2s | < 2s | < 2s |
| Email Send | < 5s | < 5s | < 5s |
| **Total** | **~3–10 min** | **~3–10 min** | **~10–25 min** |

Hardware assumptions: 16GB RAM, modern CPU, no GPU (CPU-only Whisper).

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `Ollama is not running` | ollama serve not started | Run `ollama serve` first |
| `Model not found` | Model not pulled | Run `ollama pull gpt-oss:120b-cloud` |
| `Watch path does not exist` | Path misconfigured | Check `settings.yaml` |
| `Teams webhook 400 error` | Malformed card JSON | Check Teams schema version |
| `SMTP auth failed` | Wrong credentials | Use App Password, not account password |
| `JSON parse failed` | LLM bad output | Usually self-heals on retry; check logs |
| `File not processed` | Already in DB as completed | Use `--retry-failed` or delete DB |
| `OneDrive files appear twice` | Temp file then rename | `on_moved` handler covers this |
