"""Logical identity helpers for queued lead notifications."""

from collections import OrderedDict
import re
from typing import Any, Dict, Iterable, List, Optional

from .phone_utils import normalize_phone_strict


def unwrap_lead_data(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return the lead payload from either a queue document or a raw payload."""
    lead_data = item.get("lead_data") if isinstance(item, dict) else None
    return lead_data if isinstance(lead_data, dict) else item


def lead_notification_identity(item: Dict[str, Any]) -> Optional[str]:
    """Build an identity for a contact/property, independently of alert type."""
    lead_data = unwrap_lead_data(item)
    prospecto = lead_data.get("prospecto") or {}

    raw_phone = (
        lead_data.get("lead_phone")
        or lead_data.get("phone")
        or lead_data.get("whatsapp_phone")
        or ""
    )
    normalized_phone = normalize_phone_strict(str(raw_phone))
    phone = re.sub(r"\D", "", normalized_phone or str(raw_phone))

    raw_code = (
        lead_data.get("property_code")
        or lead_data.get("codigo")
        or prospecto.get("codigo")
        or prospecto.get("codigo_interno")
        or ""
    )
    property_code = re.sub(r"[^0-9a-z]", "", str(raw_code).casefold())
    if property_code in {"nd", "sn", "none"}:
        property_code = ""

    if phone:
        return f"phone:{phone}|property:{property_code}"

    email = str(lead_data.get("email") or prospecto.get("email") or "").strip().casefold()
    if email:
        return f"email:{email}|property:{property_code}"

    return None


def deduplicate_lead_notifications(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep one logical lead and retain all queue ids that must be marked sent."""
    unique_by_key: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    for position, original in enumerate(items):
        item = dict(original)
        identity = lead_notification_identity(item)
        key = identity or f"queue-item:{item.get('_id', position)}"

        if key not in unique_by_key:
            item["_notification_ids"] = [item["_id"]] if item.get("_id") is not None else []
            unique_by_key[key] = item
            continue

        duplicate_id = item.get("_id")
        if duplicate_id is not None:
            unique_by_key[key]["_notification_ids"].append(duplicate_id)

    return list(unique_by_key.values())
