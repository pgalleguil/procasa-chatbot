"""Conservative capture of explicit customer requests for future price alerts.

A request is an internal review task, NOT permission to run a campaign or
guarantee that an automatic price alert will be sent.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from .phone_utils import normalize_phone_strict

COLLECTION = "leads"
WATCH_ROOT = "price_drop_v2.watch_requests"


def _plain(text: str) -> str:
    value = "".join(
        ch for ch in unicodedata.normalize("NFKD", str(text or "").casefold())
        if not unicodedata.combining(ch)
    )
    return re.sub(r"\s+", " ", value).strip()


def asks_for_price_drop_notice(text: str) -> bool:
    """Require both future price reduction and an explicit notice request."""
    p = _plain(text)
    if re.search(r"\b(?:no me avis\w*|no quiero que me avis\w*|no me notific\w*|"
                 r"no me inform\w*|no me manden avisos)\b", p):
        return False
    notice = re.search(r"\b(?:avis\w*|notific\w*|inform\w*|alert\w*)\b", p)
    reduction = re.search(r"\b(?:baj\w*|rebaj\w*|reducc\w*|descuent\w*)\b", p)
    return bool(notice and reduction)


def register_price_drop_watch(
    db, *, lead_id, phone: str, property_code: str,
    customer_text: str, now: datetime | None = None,
) -> dict:
    """Idempotent, property-specific review record. Never sends WhatsApp."""
    code = str(property_code or "").strip()
    canonical = normalize_phone_strict(phone)
    if not re.fullmatch(r"\d{4,6}", code) or not canonical or not lead_id:
        return {"status": "MISSING_CONTEXT"}
    if not asks_for_price_drop_notice(customer_text):
        return {"status": "NOT_REQUESTED"}
    if not db["leads"].find_one({"_id": lead_id, "phone": phone}, {"_id": 1}):
        return {"status": "LEAD_NOT_VERIFIED"}
    if not db["universo_cartera_prop360"].find_one(
        {"codigo": code}, {"codigo": 1},
    ):
        return {"status": "PROPERTY_NOT_VERIFIED"}

    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("timezone_required")
    # The lead itself is the source of truth. One idempotent, atomic
    # property-scoped slot per customer; no campaign/watch/dispatch collection.
    # A previous unsubscribe or fulfilled request is NEVER re-enabled by
    # repeating the same chat message.
    field = f"{WATCH_ROOT}.{code}"
    record = {
        "property_code": code,
        "requested_at": stamp,
        "customer_request": str(customer_text)[:450],
        "source": "whatsapp_explicit_customer_request",
        "status": "PENDING_HUMAN_REVIEW",
        "automatic_sending_enabled": False,
    }
    outcome = db["leads"].update_one(
        {"_id": lead_id, "phone": phone, field: {"$exists": False}},
        {"$set": {field: record}},
    )
    if outcome.modified_count == 1:
        return {"status": "RECORDED", "property_code": code}
    existing = db["leads"].find_one(
        {"_id": lead_id, "phone": phone, field: {"$exists": True}},
        {"_id": 1},
    )
    return {
        "status": "ALREADY_RECORDED" if existing else "LEAD_NOT_VERIFIED",
        "property_code": code,
    }
