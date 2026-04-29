"""
config.py — Application Configuration Loader
Reads from config/settings.yaml and overrides with environment variables.
Environment variables always win for secrets (passwords, URLs, etc.).
"""

import os
import logging
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Configuration Dataclasses
# ──────────────────────────────────────────────────────────────

@dataclass
class WatcherConfig:
    watch_path: str = ""
    delay_seconds: int = 300           # Wait before processing (file must be fully synced)
    supported_extensions: List[str] = field(
        default_factory=lambda: [".mp4", ".vtt", ".docx"]
    )


@dataclass
class OllamaConfig:
    model: str = "gpt-oss:120b-cloud"
    base_url: str = "http://localhost:11434"
    temperature: float = 0.1           # Low = deterministic, factual output
    max_retries: int = 3
    timeout: int = 300                 # 5 min — large models are slow


@dataclass
class TeamsConfig:
    webhook_url: str = ""
    enabled: bool = True


@dataclass
class EmailConfig:
    smtp_host: str = "smtp.office365.com"
    smtp_port: int = 587
    username: str = ""
    password: str = ""
    use_tls: bool = True
    sender_name: str = "AI Meeting System"
    recipients: List[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class AppConfig:
    watcher: WatcherConfig = field(default_factory=WatcherConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    teams: TeamsConfig = field(default_factory=TeamsConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    db_path: str = "data/meetings.db"
    log_level: str = "INFO"
    log_file: str = "logs/meeting_system.log"


# ──────────────────────────────────────────────────────────────
# Loader
# ──────────────────────────────────────────────────────────────

def load_config(config_path: str = "config/settings.yaml") -> AppConfig:
    """
    Load configuration from YAML file then overlay environment variables.
    Secrets (passwords, webhook URLs) should ALWAYS come from env vars,
    never hard-coded in YAML that may be committed to source control.
    """
    config = AppConfig()

    # 1. Load YAML
    yaml_path = Path(config_path)
    if yaml_path.exists():
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        w = data.get("watcher", {})
        config.watcher.watch_path      = w.get("watch_path", config.watcher.watch_path)
        config.watcher.delay_seconds   = int(w.get("delay_seconds", config.watcher.delay_seconds))

        o = data.get("ollama", {})
        config.ollama.model            = o.get("model", config.ollama.model)
        config.ollama.base_url         = o.get("base_url", config.ollama.base_url)
        config.ollama.temperature      = float(o.get("temperature", config.ollama.temperature))
        config.ollama.max_retries      = int(o.get("max_retries", config.ollama.max_retries))
        config.ollama.timeout          = int(o.get("timeout", config.ollama.timeout))

        t = data.get("teams", {})
        config.teams.webhook_url       = t.get("webhook_url", "")
        config.teams.enabled           = bool(t.get("enabled", True))

        e = data.get("email", {})
        config.email.smtp_host         = e.get("smtp_host", config.email.smtp_host)
        config.email.smtp_port         = int(e.get("smtp_port", config.email.smtp_port))
        config.email.username          = e.get("username", "")
        config.email.password          = e.get("password", "")
        config.email.use_tls           = bool(e.get("use_tls", True))
        config.email.sender_name       = e.get("sender_name", config.email.sender_name)
        config.email.recipients        = e.get("recipients", [])
        config.email.enabled           = bool(e.get("enabled", True))

        config.db_path                 = data.get("db_path", config.db_path)
        config.log_level               = data.get("log_level", config.log_level)
        config.log_file                = data.get("log_file", config.log_file)
    else:
        logger.warning(f"Config file not found: {config_path} — using defaults + env vars.")

    # 2. Overlay environment variables (secrets should live here only)
    config.teams.webhook_url  = os.getenv("TEAMS_WEBHOOK_URL",  config.teams.webhook_url)
    config.email.username     = os.getenv("EMAIL_USERNAME",     config.email.username)
    config.email.password     = os.getenv("EMAIL_PASSWORD",     config.email.password)
    config.watcher.watch_path = os.getenv("WATCH_PATH",         config.watcher.watch_path)
    config.ollama.base_url    = os.getenv("OLLAMA_BASE_URL",     config.ollama.base_url)

    return config
