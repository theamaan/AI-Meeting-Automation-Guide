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
        attendance: Dict = None,
    ) -> bool:
        """
        Build and send an HTML + plain-text multipart email.
        Returns True on success, False on any SMTP error.
        """
        if not recipients:
            logger.warning("No recipients configured — skipping email.")
            return False

        try:
            html_body  = self._render("email_template.html",       mom_data, attendance)
            plain_body = self._render("email_template_plain.txt",  mom_data, attendance)
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
                "SMTP authentication failed for %s.\n"
                "  Office 365 fix options:\n"
                "    1. Ask your IT admin to enable SMTP AUTH on your mailbox:\n"
                "       Microsoft 365 admin → Users → %s → Mail → Manage email apps\n"
                "       → tick 'Authenticated SMTP'\n"
                "    2. OR use an App Password if your account has MFA enabled:\n"
                "       myaccount.microsoft.com → Security → App passwords → New\n"
                "    3. OR set EMAIL_PASSWORD in config/.env to your App Password.",
                self.username, self.username
            )
            return False
        except smtplib.SMTPException as exc:
            logger.error("SMTP error: %s", exc)
            return False
        except Exception as exc:
            logger.error("Email send failed: %s", exc, exc_info=True)
            return False

    # ── Internal ──────────────────────────────────────────────

    def _render(self, template_name: str, mom_data: Dict, attendance: Dict = None) -> str:
        template = self.jinja_env.get_template(template_name)
        return template.render(
            mom=mom_data,
            attendance=attendance or {},
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

    # ── Feature additions ─────────────────────────────────────

    def send_sentiment_email(
        self,
        sentiment_data: Dict,
        mom_data: Dict,
        manager_email: str,
    ) -> bool:
        """
        Send a confidential morale report to the manager only (Feature 8).
        Never sent to the full team — single recipient only.
        Returns True on success, False on any SMTP error.
        """
        if not manager_email:
            logger.warning("No manager_email configured — skipping morale report.")
            return False
        try:
            template = self.jinja_env.get_template("email_template_sentiment.html")
            html_body = template.render(
                sentiment=sentiment_data,
                mom=mom_data,
                generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            title  = mom_data.get("meeting_title", "Team Meeting")
            date   = mom_data.get("meeting_date",  datetime.now().strftime("%Y-%m-%d"))
            flags  = sentiment_data.get("flags_count", 0)
            subject = (
                f"[CONFIDENTIAL] Morale Report — {title} — {date}"
                + (f" — ⚑ {flags} flag(s)" if flags else "")
            )
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"{self.sender_name} <{self.username}>"
            msg["To"]      = manager_email
            msg.attach(MIMEText(html_body, "html", "utf-8"))
            self._smtp_send(msg, [manager_email])
            logger.info("Morale report sent to manager: %s", manager_email)
            return True
        except smtplib.SMTPException as exc:
            logger.error("SMTP error sending morale report: %s", exc)
            return False
        except Exception as exc:
            logger.error("Morale report send failed: %s", exc, exc_info=True)
            return False

    def send_draft_approval_email(
        self,
        mom_data: Dict,
        organizer_email: str,
        approve_url: str,
        reject_url: str,
        timeout_minutes: int,
    ) -> bool:
        """
        Send a draft MOM to the organizer for approval before team delivery (Feature 9).
        Returns True on success, False on any SMTP error.
        """
        if not organizer_email:
            logger.warning("No organizer_email configured — skipping approval draft.")
            return False
        try:
            template = self.jinja_env.get_template("email_draft_approval.html")
            html_body = template.render(
                mom=mom_data,
                approve_url=approve_url,
                reject_url=reject_url,
                timeout_minutes=timeout_minutes,
                generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            title   = mom_data.get("meeting_title", "Team Meeting")
            date    = mom_data.get("meeting_date",  datetime.now().strftime("%Y-%m-%d"))
            subject = f"[ACTION REQUIRED] Approve MOM Draft — {title} — {date}"
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"{self.sender_name} <{self.username}>"
            msg["To"]      = organizer_email
            msg.attach(MIMEText(html_body, "html", "utf-8"))
            self._smtp_send(msg, [organizer_email])
            logger.info("Draft approval email sent to organizer: %s", organizer_email)
            return True
        except smtplib.SMTPException as exc:
            logger.error("SMTP error sending draft approval email: %s", exc)
            return False
        except Exception as exc:
            logger.error("Draft approval email send failed: %s", exc, exc_info=True)
            return False

    def send_digest_email(
        self,
        participant_name: str,
        digest_data: Dict,
        recipient_email: str,
        date_range: Dict,
    ) -> bool:
        """
        Send a personal weekly productivity digest to one participant (Feature 10).
        Returns True on success, False on any SMTP error.
        """
        if not recipient_email:
            logger.warning("No email for %s — skipping digest.", participant_name)
            return False
        try:
            template = self.jinja_env.get_template("email_digest.html")
            html_body = template.render(
                participant_name=participant_name,
                data=digest_data,
                date_range=date_range,
                generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            start   = date_range.get("start", "")
            end     = date_range.get("end",   "")
            subject = f"📊 Your Week in Review — {start} to {end}"
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"{self.sender_name} <{self.username}>"
            msg["To"]      = recipient_email
            msg.attach(MIMEText(html_body, "html", "utf-8"))
            self._smtp_send(msg, [recipient_email])
            logger.info("Digest sent to %s (%s)", participant_name, recipient_email)
            return True
        except smtplib.SMTPException as exc:
            logger.error("SMTP error sending digest to %s: %s", participant_name, exc)
            return False
        except Exception as exc:
            logger.error("Digest send failed for %s: %s", participant_name, exc, exc_info=True)
            return False
