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
import time
from copy import deepcopy
from datetime import datetime
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)


class TeamsNotifier:

    MAX_TEAMS_PAYLOAD_BYTES = 28 * 1024
    TARGET_PAYLOAD_BYTES = 26 * 1024  # Safety margin below Teams hard limit
    PART_POST_DELAY_SECONDS = 1.5      # Helps preserve visible order in async Flow posting

    def __init__(self, webhook_url: str, timeout: int = 30):
        self.webhook_url = webhook_url
        self.timeout = timeout

    # ── Public API ────────────────────────────────────────────

    def send_mom_card(self, mom_data: Dict, attendance: Dict = None) -> bool:
        """Build Adaptive Card from MOM dict and send to Teams webhook."""
        compact_mom = self._compact_mom(deepcopy(mom_data))
        payload = self._build_payload(compact_mom, attendance=attendance)
        size_bytes = self._payload_size(payload)

        if size_bytes <= self.TARGET_PAYLOAD_BYTES:
            return self._post(payload)

        participants = compact_mom.get("participants", [])
        if not participants:
            logger.error(
                "Teams payload too large (%d bytes) and no participants to split.",
                size_bytes,
            )
            return False

        logger.warning(
            "Teams payload too large (%d bytes). Splitting into multiple cards under %d bytes.",
            size_bytes,
            self.TARGET_PAYLOAD_BYTES,
        )

        chunks = self._chunk_participants_by_size(compact_mom, attendance=attendance)
        title = compact_mom.get("meeting_title", "Daily Standup Report")
        all_sent = True

        for idx, chunk in enumerate(chunks, start=1):
            part_mom = deepcopy(compact_mom)
            part_mom["participants"] = chunk
            if len(chunks) > 1:
                part_mom["meeting_title"] = f"{title} (Part {idx}/{len(chunks)})"
                # Keep decisions only in first card to reduce duplicate payload size.
                if idx > 1:
                    part_mom["key_decisions"] = []

            part_payload = self._build_payload(part_mom, attendance=attendance)
            part_size = self._payload_size(part_payload)
            if part_size > self.MAX_TEAMS_PAYLOAD_BYTES:
                logger.error(
                    "Card part %d/%d is still too large (%d bytes > %d).",
                    idx,
                    len(chunks),
                    part_size,
                    self.MAX_TEAMS_PAYLOAD_BYTES,
                )
                all_sent = False
                continue

            logger.info(
                "Sending Teams card part %d/%d (%d participants, %d bytes).",
                idx,
                len(chunks),
                len(chunk),
                part_size,
            )
            all_sent = self._post(part_payload) and all_sent
            # Workflows posting is asynchronous (HTTP 202). A small delay between parts
            # reduces out-of-order arrival in chat (e.g. 2/3 before 1/3).
            if idx < len(chunks):
                time.sleep(self.PART_POST_DELAY_SECONDS)

        return all_sent

    def _payload_size(self, payload: Dict) -> int:
        """Return payload size in bytes as transmitted over HTTP JSON body."""
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def _chunk_participants_by_size(self, mom: Dict, attendance: Dict = None) -> List[List[Dict]]:
        """Greedily split participants so each generated card stays below target size."""
        participants = mom.get("participants", [])
        chunks: List[List[Dict]] = []
        current_chunk: List[Dict] = []

        for person in participants:
            candidate = current_chunk + [person]
            candidate_mom = deepcopy(mom)
            candidate_mom["participants"] = candidate
            candidate_payload = self._build_payload(candidate_mom, attendance=attendance)
            candidate_size = self._payload_size(candidate_payload)

            if candidate_size <= self.TARGET_PAYLOAD_BYTES or not current_chunk:
                current_chunk = candidate
            else:
                chunks.append(current_chunk)
                current_chunk = [person]

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def _compact_mom(self, mom: Dict) -> Dict:
        """Trim verbose fields to keep adaptive card payload within Teams limits."""

        def _trim_text(text: str, max_len: int) -> str:
            if not isinstance(text, str):
                return ""
            clean = re.sub(r"\s+", " ", text).strip()
            return clean if len(clean) <= max_len else (clean[: max_len - 1] + "…")

        def _trim_list(items: List[str], max_items: int, max_item_len: int) -> List[str]:
            if not isinstance(items, list):
                return []
            return [_trim_text(str(item), max_item_len) for item in items[:max_items] if str(item).strip()]

        mom["status_reason"] = _trim_text(mom.get("status_reason", ""), 220)
        mom["team_summary"] = _trim_text(mom.get("team_summary", ""), 700)

        decisions = mom.get("key_decisions", [])
        mom["key_decisions"] = _trim_list(decisions, max_items=8, max_item_len=140)

        participants = mom.get("participants", [])
        if isinstance(participants, list):
            for person in participants:
                person["name"] = _trim_text(person.get("name", "Unknown"), 60)
                person["progress_summary"] = _trim_text(person.get("progress_summary", ""), 240)
                person["yesterday"] = _trim_list(person.get("yesterday", []), max_items=4, max_item_len=110)
                person["today"] = _trim_list(person.get("today", []), max_items=4, max_item_len=110)
                person["action_items"] = _trim_list(person.get("action_items", []), max_items=4, max_item_len=110)
                person["blockers"] = _trim_list(person.get("blockers", []), max_items=3, max_item_len=110)

        return mom

    # ── Card Builder ──────────────────────────────────────────

    def _build_payload(self, mom: Dict, attendance: Dict = None) -> Dict:  # noqa: C901
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

        # ── Build per-person attendance lookup ─────────────────
        att_lookup: Dict[str, str] = {}
        if attendance:
            for cat in ("spoke", "silent", "absent", "unknown"):
                for n in attendance.get(cat, []):
                    att_lookup[n.strip().lower()] = cat

        # ── Stats ─────────────────────────────────────────────
        total         = len(participants)
        blocker_count = sum(1 for p in participants if p.get("blockers"))
        stats_parts   = []

        if attendance and attendance.get("has_csv"):
            spoke_n  = len(attendance.get("spoke", []))
            silent_n = len(attendance.get("silent", []))
            absent_n = len(attendance.get("absent", []))
            if spoke_n:
                stats_parts.append(f"{spoke_n} 💬 spoke")
            if silent_n:
                stats_parts.append(f"{silent_n} 🔇 silent")
            if absent_n:
                stats_parts.append(f"{absent_n} ⬜ absent")
        else:
            present_count = sum(1 for p in participants if p.get("yesterday") or p.get("today"))
            absent_count  = total - present_count
            if present_count:
                stats_parts.append(f"{present_count} present")
            if absent_count:
                stats_parts.append(f"{absent_count} absent")

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
            att_status   = att_lookup.get(name.strip().lower(), "spoke")

            # Dot color: absent=⬜, silent=🔇, blocker=🔴, clear=🟢
            if att_status == "absent":
                dot = "⬜"
            elif att_status in ("silent", "unknown"):
                dot = "🔇"
            elif has_blockers:
                dot = "🔴"
            else:
                dot = "🟢"

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
            decision_bullets = [
                {
                    "type": "TextBlock",
                    "text": f"•  {d}",
                    "wrap": True,
                    "spacing": "Small",
                    "size": "Small",
                }
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
                        "size": "Medium",
                        "spacing": "None"
                    },
                    *decision_bullets,
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
            # 200 = old Connector webhook success (message posted immediately)
            if resp.status_code == 200:
                logger.info("Teams card sent successfully (HTTP 200).")
                return True

            # 202 = new Power Automate Workflows webhook accepted
            # This means the workflow trigger accepted the request; posting can still fail later.
            if resp.status_code == 202:
                tracking_headers = {
                    k: v
                    for k, v in resp.headers.items()
                    if k.lower() in ("x-ms-workflow-run-id", "x-ms-client-tracking-id", "x-ms-request-id")
                }
                logger.info(
                    "Teams webhook accepted payload (HTTP 202). Delivery is asynchronous via Power Automate."
                )
                if tracking_headers:
                    logger.info("Workflow tracking headers: %s", tracking_headers)
                if resp.text:
                    logger.info("Webhook response body (first 300 chars): %s", resp.text[:300])
                return True

            if resp.status_code == 413:
                logger.error(
                    "Teams/Flow rejected payload (HTTP 413 RequestEntityTooLarge). "
                    "Adaptive card payload must be under 28KB."
                )
                logger.error("Response body: %s", resp.text[:300])
                return False

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





