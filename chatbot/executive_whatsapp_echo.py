"""Correlate one CRM-accepted WhatsApp send with its provider webhook echo.

WaSender send-message may return a numeric API receipt while messages.upsert
uses a WhatsApp message key. These are DIFFERENT identifiers for the same
message. We only join a uniquely matching, owned, recent, same-session outgoing
message. This helper never looks at inbound/customer text or private chats.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable


ECHO_WINDOW = timedelta(seconds=30)


def _utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc)


def _normalized(text: object) -> str:
    return " ".join(text.split()) if isinstance(text, str) else ""


def find_unique_echo_match(
    messages: Iterable[dict],
    *,
    source: str,
    content: str,
    session_id: str,
    cycle_id: str,
    owner_id: str,
    occurred_at: datetime,
) -> dict | None:
    """Only a unique unpaired candidate may represent this exact outgoing send."""
    at = _utc(occurred_at)
    if at is None or not _normalized(content):
        return None
    matched = []
    for message in messages or ():
        if not isinstance(message, dict) or message.get("source") != source:
            continue
        if str(message.get("wasender_session_id") or "") != session_id:
            continue
        if str(message.get("assignment_cycle_id") or "") != cycle_id:
            continue
        if str(message.get("assigned_executive_id") or "") != owner_id:
            continue
        if message.get("channel") != "executive_whatsapp":
            continue
        if not message.get("message_id") or not message.get("provider_message_id"):
            continue
        if message.get("role") != ("assistant" if source == "crm_hot_auto_api" else "agent"):
            continue
        if _normalized(message.get("content")) != _normalized(content):
            continue
        when = _utc(message.get("timestamp"))
        if when is None or abs(at - when) > ECHO_WINDOW:
            continue
        # Each correspondence can claim only one echo; a later genuine
        # identical message must remain a separate event.
        if source in {"crm_manual_api", "crm_hot_auto_api"}:
            if message.get("provider_echo_message_id"):
                continue
        elif source == "whatsapp_direct":
            if message.get("crm_api_provider_message_id"):
                continue
        else:
            continue
        matched.append(message)
        if len(matched) > 1:
            return None
    return matched[0] if len(matched) == 1 else None
