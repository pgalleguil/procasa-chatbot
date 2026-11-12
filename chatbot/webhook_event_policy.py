"""Small ingress predicates for provider webhook events.

WaSender currently documents X-Webhook-Signature as the webhook secret itself.
Earlier integrations can use HMAC-SHA256(raw_body, secret). Verify either
documented form using timing-safe comparison; never accept missing or unknown
signatures while authentication is enabled.
"""
from __future__ import annotations

import hashlib
import hmac


OUTBOUND_MESSAGE_EVENTS = frozenset({
    "message.sent",
    "messages.sent",
})


def verify_wasender_webhook_signature(
    raw_body: bytes, supplied_header: str | None, webhook_secret: str | None
) -> bool:
    """Verify exact provider secret or the legacy HMAC-SHA256 hex signature."""
    secret = str(webhook_secret or "").strip()
    candidate = str(supplied_header or "").strip()
    if not secret or not candidate or not isinstance(raw_body, bytes):
        return False
    # Official WaSender webhook setup and help center: signature is the shared
    # webhook secret value (not an HMAC digest).
    if hmac.compare_digest(candidate.encode("utf-8"), secret.encode("utf-8")):
        return True
    legacy_hex = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if candidate.lower().startswith("sha256="):
        candidate = candidate[7:]
    return hmac.compare_digest(candidate.encode("utf-8"), legacy_hex.encode("ascii"))


def is_outbound_message_event(payload: dict | None) -> bool:
    """Return true for provider events that must never enter chatbot AI."""
    if not isinstance(payload, dict):
        return False
    candidates = [payload.get("event")]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("event"))
    return any(str(value or "").strip().casefold() in OUTBOUND_MESSAGE_EVENTS for value in candidates)
