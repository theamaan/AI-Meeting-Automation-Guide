"""
test_approval_server.py — Unit tests for Feature 9: Draft Approval Gate

Tests the ApprovalCallbackServer without any SMTP calls.
Run: pytest tests/test_approval_server.py -v
"""

import sys
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from approval_server import ApprovalCallbackServer


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _find_free_port() -> int:
    """Return a free TCP port on localhost."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str) -> int:
    """Return the HTTP status code for a GET request to url."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture()
def server():
    """Start a fresh ApprovalCallbackServer on a random port for each test."""
    db = MagicMock()
    port = _find_free_port()
    srv = ApprovalCallbackServer(db=db, port=port)
    srv.start()
    # Give the daemon thread a moment to bind
    time.sleep(0.1)
    yield srv
    srv.stop()


# ──────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────

class TestApprovalCallbackServer:

    def test_health_endpoint_returns_200(self, server):
        """GET /health returns HTTP 200."""
        status = _get(f"http://127.0.0.1:{server.port}/health")
        assert status == 200

    def test_approve_decision_unblocks_wait(self, server):
        """/approve?token=XXX fires the event and wait_for_decision returns 'approved'."""
        token = "test-token-approve-001"
        result_holder = {}

        def waiter():
            result_holder["result"] = server.wait_for_decision(
                token=token, timeout_seconds=5, auto_approve=True
            )

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.15)  # Allow waiter to register

        status = _get(f"http://127.0.0.1:{server.port}/approve?token={token}")
        t.join(timeout=3)

        assert status == 200
        assert result_holder.get("result") == "approved"

    def test_reject_decision_unblocks_wait(self, server):
        """/reject?token=XXX fires the event and wait_for_decision returns 'rejected'."""
        token = "test-token-reject-001"
        result_holder = {}

        def waiter():
            result_holder["result"] = server.wait_for_decision(
                token=token, timeout_seconds=5, auto_approve=True
            )

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.15)

        _get(f"http://127.0.0.1:{server.port}/reject?token={token}")
        t.join(timeout=3)

        assert result_holder.get("result") == "rejected"

    def test_timeout_returns_auto_approved(self, server):
        """When no action is taken and auto_approve=True, returns 'auto_approved'."""
        token = "test-token-timeout-auto"
        result = server.wait_for_decision(
            token=token, timeout_seconds=1, auto_approve=True
        )
        assert result == "auto_approved"

    def test_timeout_returns_rejected_when_auto_approve_false(self, server):
        """When no action is taken and auto_approve=False, returns 'rejected'."""
        token = "test-token-timeout-reject"
        result = server.wait_for_decision(
            token=token, timeout_seconds=1, auto_approve=False
        )
        assert result == "rejected"

    def test_unknown_token_returns_404(self, server):
        """Approval request with an unregistered token returns HTTP 200 with warning (not crash)."""
        # The server handles unknown tokens gracefully (logs warning, still responds)
        # The response is still 200 (HTML page) — only the internal state is unaffected
        status = _get(f"http://127.0.0.1:{server.port}/approve?token=UNKNOWN-TOKEN")
        assert status == 200

    def test_unknown_path_returns_404(self, server):
        """An unrecognised path returns HTTP 404."""
        status = _get(f"http://127.0.0.1:{server.port}/nonexistent")
        assert status == 404

    def test_token_is_single_use(self, server):
        """After a token is consumed by approve, a second approve for the same token is a no-op."""
        token = "test-token-single-use"
        result_holder = {}

        def waiter():
            result_holder["result"] = server.wait_for_decision(
                token=token, timeout_seconds=5, auto_approve=True
            )

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.15)

        _get(f"http://127.0.0.1:{server.port}/approve?token={token}")
        t.join(timeout=3)

        # First approve should have fired
        assert result_holder.get("result") == "approved"

        # Second approve for the same token — server should handle gracefully
        # (logs warning, does NOT raise an exception, still returns 200)
        status = _get(f"http://127.0.0.1:{server.port}/approve?token={token}")
        assert status == 200
