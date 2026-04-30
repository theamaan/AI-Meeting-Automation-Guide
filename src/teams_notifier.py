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

    def _build_payload(self, mom: Dict) -> Dict:  # noqa: C901
        participants  = mom.get("participants", [])
        status        = mom.get("overall_status", "ALL_CLEAR")
        status_reason = mom.get("status_reason", "")
        team_summary  = mom.get("team_summary", "No summary available.")
        title         = mom.get("meeting_title", "Daily Standup Report")
        date          = mom.get("meeting_date", datetime.now().strftime("%B %d, %Y"))
        decisions     = mom.get("key_decisions", [])

        is_clear      = status == "ALL_CLEAR"
        status_emoji  = "✅" if is_clear else "⚠️"
        status_label  = "ALL CLEAR" if is_clear else "HAS ISSUES"
        status_color  = "Good" if is_clear else "Attention"

        # ── Stats ─────────────────────────────────────────────
        total         = len(participants)
        blocker_count = sum(1 for p in participants if p.get("blockers"))
        present_count = sum(1 for p in participants if p.get("yesterday") or p.get("today"))
        absent_count  = total - present_count
        stats_parts   = []
        if present_count: stats_parts.append(f"{present_count} present")
        if absent_count:  stats_parts.append(f"{absent_count} absent")
        if blocker_count:
            stats_parts.append(f"{blocker_count} 🔴 blocker{'s' if blocker_count != 1 else ''}")
        stats_text = "  •  ".join(stats_parts) if stats_parts else f"{total} participants"

        # Pre-compute all panel IDs for accordion targeting
        all_pids = [self._safe_id(p.get("name", "")) for p in participants]

        body: List[Dict] = []

        # ── Header ───────────────────────────────────────────
        body.append({
            "type": "Container",
            "style": "emphasis",
            "bleed": True,
            "items": [
                {
                    "type": "ColumnSet",
                    "columns": [
                        {
                            "type": "Column",
                            "width": "stretch",
                            "items": [
                                {
                                    "type": "TextBlock",
                                    "text": f"📋  {title}",
                                    "size": "Large",
                                    "weight": "Bolder",
                                    "color": "Accent",
                                    "wrap": True
                                },
                                {
                                    "type": "TextBlock",
                                    "text": date,
                                    "size": "Small",
                                    "isSubtle": True,
                                    "spacing": "None"
                                }
                            ]
                        },
                        {
                            "type": "Column",
                            "width": "auto",
                            "verticalContentAlignment": "Top",
                            "items": [{
                                "type": "TextBlock",
                                "text": f"{status_emoji}  {status_label}",
                                "color": status_color,
                                "weight": "Bolder",
                                "size": "Small",
                                "horizontalAlignment": "Right"
                            }]
                        }
                    ]
                },
                {
                    "type": "TextBlock",
                    "text": stats_text,
                    "size": "Small",
                    "isSubtle": True,
                    "spacing": "Small",
                    "wrap": True
                }
            ]
        })

        # ── Issues banner ─────────────────────────────────────
        if not is_clear and status_reason:
            body.append({
                "type": "Container",
                "style": "attention",
                "spacing": "None",
                "bleed": True,
                "items": [{
                    "type": "TextBlock",
                    "text": f"⚠️  {status_reason}",
                    "wrap": True,
                    "color": "Attention",
                    "size": "Small"
                }]
            })

        # ── Team summary ──────────────────────────────────────
        body.append({
            "type": "Container",
            "spacing": "Medium",
            "separator": True,
            "items": [
                {
                    "type": "TextBlock",
                    "text": "🏢  Team Summary",
                    "weight": "Bolder",
                    "size": "Medium",
                    "spacing": "None"
                },
                {
                    "type": "TextBlock",
                    "text": team_summary,
                    "wrap": True,
                    "isSubtle": True,
                    "size": "Small",
                    "spacing": "Small"
                }
            ]
        })

        # ── Member selector header ────────────────────────────
        body.append({
            "type": "TextBlock",
            "text": "👥  TEAM MEMBERS  —  tap a name to expand",
            "weight": "Bolder",
            "size": "Small",
            "isSubtle": True,
            "spacing": "Medium",
            "separator": True
        })

        # ── Person buttons ────────────────────────────────────
        # Accordion technique: each button's Action.ToggleVisibility uses
        # object targets {"elementId": "...", "isVisible": bool} instead of
        # plain string IDs. This FORCES the target to a specific state rather
        # than toggling — so clicking Alice always shows Alice AND always
        # hides Bob, Carol, etc. True one-at-a-time accordion behavior.
        button_columns = []
        for person in participants:
            name         = person.get("name", "Unknown")
            pid          = self._safe_id(name)
            has_blockers = bool(person.get("blockers"))
            dot          = "🔴" if has_blockers else "🟢"
            first_name   = name.split()[0] if name else name

            accordion_targets = (
                [{"elementId": f"details_{pid}", "isVisible": True}]
                + [
                    {"elementId": f"details_{other}", "isVisible": False}
                    for other in all_pids if other != pid
                ]
            )

            button_columns.append({
                "type": "Column",
                "width": "stretch",
                "style": "emphasis",
                "selectAction": {
                    "type": "Action.ToggleVisibility",
                    "targetElements": accordion_targets
                },
                "items": [
                    {
                        "type": "TextBlock",
                        "text": dot,
                        "horizontalAlignment": "Center",
                        "spacing": "Small",
                        "size": "Small"
                    },
                    {
                        "type": "TextBlock",
                        "text": first_name,
                        "weight": "Bolder",
                        "horizontalAlignment": "Center",
                        "size": "Small",
                        "spacing": "None",
                        "wrap": False
                    }
                ]
            })

        # Pad last row to full 3 columns so the grid looks clean
        remainder = len(button_columns) % 3
        if remainder:
            for _ in range(3 - remainder):
                button_columns.append({"type": "Column", "width": "stretch", "items": []})

        for i in range(0, len(button_columns), 3):
            body.append({
                "type": "ColumnSet",
                "spacing": "Small",
                "columns": button_columns[i : i + 3]
            })

        # ── Member detail panels ──────────────────────────────
        # All hidden by default. The accordion buttons above reveal them one
        # at a time. Each panel also has its own ✕ Close button.
        for person in participants:
            name         = person.get("name", "Unknown")
            pid          = self._safe_id(name)
            has_blockers = bool(person.get("blockers"))
            n_blockers   = len(person.get("blockers", []))
            blocker_text  = (
                f"🔴  {n_blockers} blocker{'s' if n_blockers != 1 else ''}"
                if has_blockers else "🟢  No blockers"
            )
            blocker_color = "Attention" if has_blockers else "Good"

            panel_items: List[Dict] = [
                # Panel header: name + status (left), ✕ Close (right)
                {
                    "type": "ColumnSet",
                    "spacing": "Small",
                    "columns": [
                        {
                            "type": "Column",
                            "width": "stretch",
                            "verticalContentAlignment": "Center",
                            "items": [
                                {
                                    "type": "TextBlock",
                                    "text": name,
                                    "weight": "Bolder",
                                    "size": "Medium",
                                    "spacing": "None",
                                    "wrap": True
                                },
                                {
                                    "type": "TextBlock",
                                    "text": blocker_text,
                                    "color": blocker_color,
                                    "size": "Small",
                                    "spacing": "None"
                                }
                            ]
                        },
                        {
                            "type": "Column",
                            "width": "auto",
                            "verticalContentAlignment": "Top",
                            # Force-hide only this panel on click
                            "selectAction": {
                                "type": "Action.ToggleVisibility",
                                "targetElements": [
                                    {"elementId": f"details_{pid}", "isVisible": False}
                                ]
                            },
                            "items": [{
                                "type": "TextBlock",
                                "text": "✕  Close",
                                "color": "Accent",
                                "size": "Small",
                                "horizontalAlignment": "Right",
                                "isSubtle": True
                            }]
                        }
                    ]
                }
            ]

            if person.get("progress_summary"):
                panel_items.append({
                    "type": "TextBlock",
                    "text": person["progress_summary"],
                    "wrap": True,
                    "isSubtle": True,
                    "size": "Small",
                    "spacing": "Small"
                })

            if person.get("yesterday"):
                panel_items.append(
                    self._section_block("📅  Yesterday", person["yesterday"], "Default")
                )
            if person.get("today"):
                panel_items.append(
                    self._section_block("🎯  Today", person["today"], "Accent")
                )
            if person.get("action_items"):
                panel_items.append(
                    self._section_block("✅  Action Items", person["action_items"], "Warning")
                )
            if person.get("blockers"):
                panel_items.append(
                    self._section_block("🚫  Blockers", person["blockers"], "Attention")
                )

            body.append({
                "type": "Container",
                "id": f"details_{pid}",
                "isVisible": False,
                "style": "emphasis",
                "spacing": "Small",
                "separator": True,
                "items": panel_items
            })

        # ── Key decisions ─────────────────────────────────────
        if decisions:
            body.append({
                "type": "Container",
                "spacing": "Medium",
                "separator": True,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "🔑  Key Decisions",
                        "weight": "Bolder",
                        "size": "Medium",
                        "spacing": "None"
                    },
                    {
                        "type": "FactSet",
                        "spacing": "Small",
                        "facts": [
                            {"title": f"{i + 1}.", "value": d}
                            for i, d in enumerate(decisions)
                        ]
                    }
                ]
            })

        # ── Footer ────────────────────────────────────────────
        body.append({
            "type": "Container",
            "style": "emphasis",
            "spacing": "Medium",
            "bleed": True,
            "items": [{
                "type": "TextBlock",
                "text": (
                    f"AI Meeting Intelligence System  •  "
                    f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                ),
                "size": "Small",
                "isSubtle": True,
                "horizontalAlignment": "Center",
                "wrap": True
            }]
        })

        # ── Card-level action: close all open panels ──────────
        close_all_targets = [
            {"elementId": f"details_{pid}", "isVisible": False}
            for pid in all_pids
        ]

        adaptive_card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": body,
            "actions": [
                {
                    "type": "Action.ToggleVisibility",
                    "title": "✕  Close All",
                    "targetElements": close_all_targets
                }
            ],
            "msteams": {
                "width": "Full"
            }
        }

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

    # ── Section builders ──────────────────────────────────────

    def _section_block(self, title: str, items: List[str], color: str) -> Dict:
        """Titled bullet-list section inside a detail panel."""
        bullets = [
            {
                "type": "TextBlock",
                "text": f"›  {item}",
                "wrap": True,
                "spacing": "None",
                "size": "Small",
                "color": color if color in ("Attention", "Warning") else "Default",
                "isSubtle": color not in ("Attention", "Warning", "Accent")
            }
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
                    "size": "Small",
                    "spacing": "Small"
                },
                *bullets
            ]
        }

    def _fact_set_section(self, title: str, items: List[str], color: str) -> Dict:
        """Backward-compatible alias for _section_block."""
        return self._section_block(title, items, color)

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
