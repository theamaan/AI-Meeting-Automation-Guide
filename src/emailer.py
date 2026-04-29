"""
emailer.py — Email Notification Service
Sends structured MOM emails using SMTP + Jinja2 HTML templates.
Supports Outlook (Office 365) and Gmail app passwords.

Security notes:
  - Credentials loaded from env vars only (never hard-coded)
  - STARTTLS enforced by default
  - HTML auto-escaped via Jinja2 autoescape
"""

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)


class EmailService:

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        use_tls: bool = True,
        sender_name: str = "AI Meeting System",
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.sender_name = sender_name

        # Resolve template directory relative to this file
        template_dir = Path(__file__).parent.parent / "templates"
        self.jinja_env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=select_autoescape(["html", "xml"]),  # XSS protection
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # ── Public API ────────────────────────────────────────────

    def send_mom_email(
        self,
        mom_data: Dict,
        recipients: List[str],
    ) -> bool:
        """
        Build and send an HTML + plain-text multipart email.
        Returns True on success, False on any SMTP error.
        """
        if not recipients:
            logger.warning("No recipients configured — skipping email.")
            return False

        try:
            html_body  = self._render("email_template.html",       mom_data)
            plain_body = self._render("email_template_plain.txt",  mom_data)
            subject    = self._build_subject(mom_data)

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"{self.sender_name} <{self.username}>"
            msg["To"]      = ", ".join(recipients)

            # Attach plain first — email clients prefer the last part (HTML)
            msg.attach(MIMEText(plain_body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body,  "html",  "utf-8"))

            self._smtp_send(msg, recipients)
            logger.info("MOM email sent to %d recipient(s): %s", len(recipients), recipients)
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error(
                "SMTP authentication failed. "
                "For Office 365, ensure Modern Auth is enabled or use an App Password."
            )
            return False
        except smtplib.SMTPException as exc:
            logger.error("SMTP error: %s", exc)
            return False
        except Exception as exc:
            logger.error("Email send failed: %s", exc, exc_info=True)
            return False

    # ── Internal ──────────────────────────────────────────────

    def _render(self, template_name: str, mom_data: Dict) -> str:
        template = self.jinja_env.get_template(template_name)
        return template.render(
            mom=mom_data,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _build_subject(self, mom_data: Dict) -> str:
        title  = mom_data.get("meeting_title", "Daily Standup")
        date   = mom_data.get("meeting_date",  datetime.now().strftime("%Y-%m-%d"))
        status = mom_data.get("overall_status", "")
        prefix = "⚠️" if status == "HAS_ISSUES" else "✅"
        return f"{prefix} MOM: {title} — {date}"

    def _smtp_send(self, msg: MIMEMultipart, recipients: List[str]):
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
            server.ehlo()
            if self.use_tls:
                server.starttls()
                server.ehlo()
            server.login(self.username, self.password)
            server.sendmail(self.username, recipients, msg.as_string())
