"""
approval_server.py — Draft MOM Approval Callback Server (Feature 9)

A minimal HTTP server that listens on localhost ONLY for organizer approval/reject
decisions after a draft MOM email has been sent.

Security model:
  - Binds strictly to 127.0.0.1 — never accessible from external machines.
  - Tokens are generated via secrets.token_urlsafe(32) — cryptographically random.
  - Each token is single-use; it is removed from the pending set immediately on use.
  - Server runs in a daemon thread and exits automatically when the main process exits.
"""

import http.server
import json
import logging
import threading
import urllib.parse
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class ApprovalCallbackServer:
    """
    Tiny HTTP server that handles organizer approval/reject callbacks.

    Workflow:
        1. Caller generates a token via secrets.token_urlsafe(32).
        2. Caller calls wait_for_decision(token, timeout_seconds, auto_approve).
        3. Server routes GET /approve?token=XXX or GET /reject?token=XXX.
        4. wait_for_decision returns "approved", "rejected", or "auto_approved".
    """

    def __init__(self, db, port: int = 8765):
        """
        Args:
            db:    Database instance (used by handler to validate tokens).
            port:  Local port to listen on. Default 8765.
        """
        self.db = db
        self.port = port
        self._pending: Dict[str, threading.Event] = {}
        self._results:  Dict[str, str] = {}
        self._lock = threading.Lock()
        self._server: Optional[http.server.HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ── Lifecycle ─────────────────────────────────────────────

    def start(self):
        """Start the approval HTTP server in a background daemon thread."""
        if self._server is not None:
            logger.warning("Approval server is already running.")
            return

        handler = self._make_handler()

        try:
            self._server = http.server.HTTPServer(("127.0.0.1", self.port), handler)
        except OSError as exc:
            logger.error(
                "Approval server failed to bind on port %d: %s\n"
                "  Check whether another process is already using port %d, "
                "or change approval.callback_port in settings.yaml.",
                self.port, exc, self.port,
            )
            raise

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="ApprovalServer",
            daemon=True,  # Dies automatically when the main process exits
        )
        self._thread.start()
        logger.info("Approval callback server started on http://127.0.0.1:%d", self.port)

    def stop(self):
        """Shut down the approval server cleanly."""
        if self._server is not None:
            self._server.shutdown()
            self._server = None
            logger.info("Approval callback server stopped.")

    # ── Public API ────────────────────────────────────────────

    def register_token(self, token: str):
        """Register a new token as awaiting a decision."""
        with self._lock:
            event = threading.Event()
            self._pending[token] = event

    def wait_for_decision(
        self,
        token: str,
        timeout_seconds: int,
        auto_approve: bool = True,
    ) -> str:
        """
        Block until the organizer approves or rejects, or until timeout.

        Returns:
            "approved"      — organizer clicked Approve
            "rejected"      — organizer clicked Reject
            "auto_approved" — timeout reached and auto_approve is True
            "rejected"      — timeout reached and auto_approve is False (same value)
        """
        with self._lock:
            if token not in self._pending:
                event = threading.Event()
                self._pending[token] = event
            else:
                event = self._pending[token]

        fired = event.wait(timeout=timeout_seconds)

        with self._lock:
            self._pending.pop(token, None)
            result = self._results.pop(token, None)

        if fired and result:
            return result

        # Timeout
        if auto_approve:
            logger.info("Approval timeout — auto-approving MOM for token ...%s", token[-8:])
            return "auto_approved"
        else:
            logger.info("Approval timeout — auto-rejecting MOM for token ...%s", token[-8:])
            return "rejected"

    # ── HTTP handler factory ──────────────────────────────────

    def _make_handler(self):
        """Return an HTTPRequestHandler class bound to this server instance."""
        server_ref = self  # closure reference

        class _Handler(http.server.BaseHTTPRequestHandler):

            def do_GET(self):  # noqa: N802 — stdlib naming convention
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                token  = params.get("token", [None])[0]

                if parsed.path == "/approve" and token:
                    server_ref._handle_decision(token, "approved")
                    self._respond(200, "✅ MOM Approved — Sending to team now. You may close this page.")
                elif parsed.path == "/reject" and token:
                    server_ref._handle_decision(token, "rejected")
                    self._respond(200, "❌ MOM Rejected — Draft discarded. No emails were sent.")
                elif parsed.path == "/health":
                    self._respond(200, "OK")
                else:
                    self._respond(404, "Not found. Invalid or expired approval link.")

            def _respond(self, code: int, message: str):
                body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/>
<title>MOM Approval</title>
<style>
  body{{font-family:'Segoe UI',Arial,sans-serif;display:flex;align-items:center;
       justify-content:center;height:100vh;margin:0;background:#f0f2f5;}}
  .box{{background:#fff;border-radius:8px;padding:40px;text-align:center;
        box-shadow:0 2px 12px rgba(0,0,0,.15);max-width:480px;}}
  h1{{font-size:20px;margin-bottom:12px;}}
  p{{color:#605e5c;font-size:14px;}}
</style></head>
<body><div class="box">
  <h1>{message}</h1>
  <p>You can close this browser tab.</p>
</div></body></html>"""
                encoded = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type",   "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                # Security headers
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options",        "DENY")
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, fmt, *args):
                # Redirect access logs to Python logger instead of stderr
                logger.debug("ApprovalServer: " + fmt, *args)

        return _Handler

    def _handle_decision(self, token: str, result: str):
        """Record the decision and signal the waiting thread."""
        with self._lock:
            if token not in self._pending:
                logger.warning(
                    "Received approval decision for unknown/expired token ...%s — ignored.",
                    token[-8:] if len(token) >= 8 else token,
                )
                return
            self._results[token] = result
            self._pending[token].set()

        logger.info("Approval decision recorded: %s for token ...%s", result, token[-8:])
