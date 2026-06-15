"""FastAPI webhook receiver.

Every incoming request is verified with HMAC-SHA256 against the stored webhook
secret. If the signature does not match, the request is rejected with 403 and
the payload is NEVER processed. Verified events are queued into SQLite for the
orchestrator to route.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import FastAPI, Request

from gitpilot.db.queue import Database
from gitpilot.github.client import WEBHOOK_SECRET_KEY, get_credential

logger = logging.getLogger("gitpilot.webhook")

app = FastAPI(title="GitPilot Webhook Server")

# Single shared DB handle for the server process.
_db = Database()


def _verify_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Constant-time HMAC-SHA256 verification of the GitHub payload."""
    if not signature_header or not secret:
        return False
    try:
        algo, received = signature_header.split("=", 1)
    except ValueError:
        return False
    if algo != "sha256":
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


@app.post("/webhook")
async def github_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    secret = get_credential(WEBHOOK_SECRET_KEY) or ""

    if not _verify_signature(body, signature, secret):
        client = request.client.host if request.client else "unknown"
        logger.warning("Rejected webhook: bad signature from %s", client)
        # 403 — never process an unverified payload.
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Invalid signature")

    event_type = request.headers.get("X-GitHub-Event", "unknown")
    payload_text = body.decode("utf-8", errors="replace")
    _db.enqueue_event(event_type, payload_text)
    logger.info("Queued webhook event: %s", event_type)
    return {"status": "queued"}


@app.get("/health")
async def health():
    return {"status": "running", "agent": "gitpilot"}


# Event types GitPilot listens for (used by `gitpilot init` when registering):
SUBSCRIBED_EVENTS = [
    "pull_request",         # opened, synchronize, labeled, unlabeled, reopened, closed
    "check_suite",          # completed
    "pull_request_review",  # submitted
    "push",                 # branch tracking
    "create",               # new branch or tag
    "delete",               # branch or tag deleted
]
