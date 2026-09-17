"""Small ingress predicates for provider webhook events."""

from __future__ import annotations


OUTBOUND_MESSAGE_EVENTS = frozenset({
    "message.sent",
    "messages.sent",
})


def is_outbound_message_event(payload: dict | None) -> bool:
    """Return true for provider events that must never enter chatbot AI."""
    if not isinstance(payload, dict):
        return False
    candidates = [payload.get("event")]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("event"))
    return any(str(value or "").strip().casefold() in OUTBOUND_MESSAGE_EVENTS for value in candidates)
