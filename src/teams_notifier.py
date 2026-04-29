"""
teams_notifier.py — Microsoft Teams Adaptive Card Sender
Builds and POSTs an Adaptive Card to a Teams Incoming Webhook.

Card features:
  - Header with meeting title, date, status badge
  - Team summary block
  - Per-person toggle buttons (Action.ToggleVisibility)
  - Hidden expandable containers with: yesterday / today / blockers / action items
  - "Expand All" action button at card footer
  - msteams.width = "Full" for wide display in channels

Adaptive Card spec: https://adaptivecards.io/explorer/
Teams webhook docs: https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook
"""

import json
import logging
import re
from datetime import datetime
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)


class TeamsNotifier:

    def __init__(self, webhook_url: str, timeout: int = 30):
        self.webhook_url = webhook_url
        self.timeout = timeout

    # ── Public API ────────────────────────────────────────────

    def send_mom_card(self, mom_data: Dict) -> bool:
        """Build Adaptive Card from MOM dict and send to Teams webhook."""
        payload = self._build_payload(mom_data)
        return self._post(payload)

    # ── Card Builder ──────────────────────────────────────────

    def _build_payload(self, mom: Dict) -> Dict:
        participants  = mom.get("participants", [])
        status        = mom.get("overall_status", "ALL_CLEAR")
        status_reason = mom.get("status_reason", "")
        team_summary  = mom.get("team_summary", "No summary available.")
        title         = mom.get("meeting_title", "Daily Standup Report")
        date          = mom.get("meeting_date", datetime.now().strftime("%B %d, %Y"))
        decisions     = mom.get("key_decisions", [])

        is_clear       = status == "ALL_CLEAR"
        status_color   = "good" if is_clear else "attention"
        status_label   = "✅  ALL CLEAR" if is_clear else "⚠️  HAS ISSUES"

        body: List[Dict] = []

        # ── Header ───────────────────────────────────────────
        body.append({
            "type": "Container",
            "style": "emphasis",
            "bleed": True,
            "items": [{
                "type": "ColumnSet",
                "columns": [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": "📋  Daily Standup Report",
                                "size": "Large",
                                "weight": "Bolder",
                                "color": "Accent"
                            },
                            {
                                "type": "TextBlock",
                                "text": f"{title}  •  {date}",
                                "size": "Small",
                                "isSubtle": True,
                                "spacing": "None",
                                "wrap": True
                            }
                        ]
                    },
                    {
                        "type": "Column",
                        "width": "auto",
                        "verticalContentAlignment": "Center",
                        "items": [{
                            "type": "TextBlock",
                            "text": status_label,
                            "color": status_color,
                            "weight": "Bolder",
                            "horizontalAlignment": "Right"
                        }]
                    }
                ]
            }]
        })

        # ── Issues banner ─────────────────────────────────────
        if not is_clear and status_reason:
            body.append({
                "type": "Container",
                "style": "attention",
                "spacing": "Small",
                "items": [{
                    "type": "TextBlock",
                    "text": f"⚠️  {status_reason}",
                    "wrap": True,
                    "color": "Attention"
                }]
            })

        # ── Team summary ──────────────────────────────────────
        body.append({
            "type": "Container",
            "spacing": "Medium",
            "items": [
                {
                    "type": "TextBlock",
                    "text": "🏢  Team Summary",
                    "weight": "Bolder",
                    "size": "Medium"
                },
                {
                    "type": "TextBlock",
                    "text": team_summary,
                    "wrap": True,
                    "isSubtle": True
                }
            ]
        })

        # ── Member buttons ────────────────────────────────────
        body.append({
            "type": "TextBlock",
            "text": "👥  Team Members — click a name to expand",
            "weight": "Bolder",
            "size": "Medium",
            "spacing": "Medium",
            "separator": True
        })

        # Build buttons in rows of 3
        button_columns = []
        for person in participants:
            name = person.get("name", "Unknown")
            pid  = self._safe_id(name)
            has_blockers = bool(person.get("blockers"))
            icon = "🔴" if has_blockers else "🟢"

            button_columns.append({
                "type": "Column",
                "width": "stretch",
                "style": "emphasis",
                "selectAction": {
                    "type": "Action.ToggleVisibility",
                    "targetElements": [f"details_{pid}"]
                },
                "items": [{
                    "type": "TextBlock",
                    "text": f"{icon}  {name}",
                    "weight": "Bolder",
                    "horizontalAlignment": "Center",
                    "wrap": False
                }]
            })

        for i in range(0, len(button_columns), 3):
            body.append({
                "type": "ColumnSet",
                "spacing": "Small",
                "columns": button_columns[i : i + 3]
            })

        # ── Member detail containers (hidden by default) ──────
        for person in participants:
            name = person.get("name", "Unknown")
            pid  = self._safe_id(name)
            has_blockers = bool(person.get("blockers"))
            header_color = "Attention" if has_blockers else "Accent"

            items: List[Dict] = [
                {
                    "type": "TextBlock",
                    "text": f"📊  {name}",
                    "weight": "Bolder",
                    "size": "Medium",
                    "color": header_color
                }
            ]

            if person.get("progress_summary"):
                items.append({
                    "type": "TextBlock",
                    "text": person["progress_summary"],
                    "wrap": True,
                    "isSubtle": True,
                    "spacing": "Small"
                })

            if person.get("yesterday"):
                items.append(self._fact_set_section(
                    "📅  Yesterday", person["yesterday"], "Good"
                ))
            if person.get("today"):
                items.append(self._fact_set_section(
                    "🎯  Today", person["today"], "Accent"
                ))
            if person.get("action_items"):
                items.append(self._fact_set_section(
                    "✅  Action Items", person["action_items"], "Warning"
                ))

            if person.get("blockers"):
                items.append(self._fact_set_section(
                    "🚫  Blockers", person["blockers"], "Attention"
                ))
            else:
                items.append({
                    "type": "TextBlock",
                    "text": "🚫  Blockers:  None",
                    "color": "Good",
                    "isSubtle": True,
                    "spacing": "Small"
                })

            body.append({
                "type": "Container",
                "id": f"details_{pid}",
                "isVisible": False,
                "style": "emphasis",
                "spacing": "Medium",
                "separator": True,
                "items": items
            })

        # ── Key decisions ─────────────────────────────────────
        if decisions:
            decision_items = [
                {"type": "TextBlock", "text": f"• {d}", "wrap": True, "spacing": "None"}
                for d in decisions
            ]
            body.append({
                "type": "Container",
                "spacing": "Medium",
                "separator": True,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "🔑  Key Decisions",
                        "weight": "Bolder",
                        "size": "Medium"
                    },
                    *decision_items
                ]
            })

        # ── Footer ────────────────────────────────────────────
        body.append({
            "type": "Container",
            "style": "emphasis",
            "spacing": "Medium",
            "items": [{
                "type": "TextBlock",
                "text": (
                    f"AI Meeting Intelligence System  •  "
                    f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                ),
                "size": "Small",
                "isSubtle": True,
                "horizontalAlignment": "Center"
            }]
        })

        # ── Build all-member IDs for "Expand All" ─────────────
        all_detail_ids = [f"details_{self._safe_id(p.get('name', ''))}" for p in participants]

        # ── Assemble card ─────────────────────────────────────
        adaptive_card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": body,
            "actions": [
                {
                    "type": "Action.ToggleVisibility",
                    "title": "📋  Expand / Collapse All Members",
                    "targetElements": all_detail_ids
                }
            ],
            "msteams": {
                "width": "Full"
            }
        }

        # Teams incoming webhook payload wraps in "attachments"
        return {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": adaptive_card
                }
            ]
        }

    # ── Section builder ───────────────────────────────────────

    def _fact_set_section(self, title: str, items: List[str], color: str) -> Dict:
        """Build a titled bullet-list container."""
        bullets = [
            {"type": "TextBlock", "text": f"• {item}", "wrap": True, "spacing": "None"}
            for item in items
        ]
        return {
            "type": "Container",
            "spacing": "Small",
            "items": [
                {
                    "type": "TextBlock",
                    "text": title,
                    "weight": "Bolder",
                    "color": color,
                    "spacing": "Small"
                },
                *bullets
            ]
        }

    # ── HTTP send ─────────────────────────────────────────────

    def _post(self, payload: Dict) -> bool:
        try:
            resp = requests.post(
                self.webhook_url,
                json=payload,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
            # 200 = old Connector webhook success
            # 202 = new Power Automate Workflows webhook accepted
            if resp.status_code in (200, 202):
                logger.info("Teams card sent successfully (HTTP %d).", resp.status_code)
                return True
            else:
                logger.error(
                    "Teams webhook returned %d: %s",
                    resp.status_code,
                    resp.text[:300],
                )
                return False
        except requests.exceptions.ConnectionError:
            logger.error(
                "Cannot reach Teams webhook. Check corporate firewall / proxy settings."
            )
            return False
        except requests.exceptions.Timeout:
            logger.error("Teams webhook request timed out.")
            return False
        except Exception as exc:
            logger.error("Teams notification error: %s", exc, exc_info=True)
            return False

    # ── Utility ───────────────────────────────────────────────

    @staticmethod
    def _safe_id(name: str) -> str:
        """Convert a person's name to a valid HTML element ID."""
        return re.sub(r"[^a-zA-Z0-9]", "_", name.lower()).strip("_")
