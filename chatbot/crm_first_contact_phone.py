"""First-contact phone disclosure: presentation/authorization only, no SLA mutation."""
from datetime import datetime, timezone
import os

from .crm_permissions import can_administer_leads

FLAG = "CRM_FIRST_CONTACT_PHONE_GATE_ENABLED"
CUTOFF = "CRM_FIRST_CONTACT_PHONE_GATE_CUTOVER_AT"
MASK = "+56 9 •••• ••••"
REF_PREFIX = "lead-id:"
OUTBOUND_SOURCES = {"crm_manual_api", "crm_hot_auto_api", "whatsapp_direct", "crm_admin_delegated_api"}


def _instant(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def enabled():
    return os.getenv(FLAG, "0") == "1" and _instant(os.getenv(CUTOFF)) is not None


def office_chatbot_origin(lead):
    """Do not confuse the portal mentioned in chat with a portal-form lead."""
    source = str(lead.get("source_type") or "").strip().lower()
    channel = str(lead.get("canal_envio") or "").strip().lower()
    if source in {"manual", "prop360", "portal", "form"} or "convecta" in channel or "manual" in channel:
        return False
    messages = lead.get("messages") or []
    return (any(m.get("role") == "user" and m.get("actor_type") == "customer" for m in messages if isinstance(m, dict))
            and any(m.get("role") == "assistant" and m.get("actor_type") == "bot"
                    and m.get("provider_message_id") for m in messages if isinstance(m, dict)))


def recorded_executive_message(lead):
    if (lead.get("first_contact_phone_access") or {}).get("verified") is True:
        return True
    for m in lead.get("messages") or []:
        if not isinstance(m, dict):
            continue
        if (m.get("channel") == "executive_whatsapp"
                and m.get("source") in OUTBOUND_SOURCES
                and m.get("role") in {"agent", "assistant"}
                and m.get("actor_type") in {"human_agent", "automation", "human_supervisor"}
                and m.get("provider_message_id") and m.get("wasender_session_id")
                and m.get("assignment_cycle_id") and m.get("assigned_executive_id")):
            return True
    return False


def cycle_id(lead):
    return str((lead.get("lifecycle") or {}).get("current_assignment_cycle_id") or "")


def phone_locked(lead, user, *, api_enabled):
    if not enabled() or not user or can_administer_leads(user.get("rol")) or not api_enabled:
        return False
    created = _instant(lead.get("created_at") or (lead.get("lifecycle") or {}).get("created_at"))
    if not created:
        from bson import ObjectId
        identifier = lead.get("_id")
        if ObjectId.is_valid(identifier):
            created = ObjectId(identifier).generation_time
    cutoff = _instant(os.getenv(CUTOFF))
    if not created or created < cutoff or not office_chatbot_origin(lead):
        return False
    if recorded_executive_message(lead):
        return False
    exception = (lead.get("first_contact_phone_access") or {}).get("contingency") or {}
    if cycle_id(lead) and exception.get("cycle_id") == cycle_id(lead):
        return False
    return True


def locked_for_user(lead, user):
    # No new provider requests. Disabled/unsupported API is a contingency.
    from .crm_executive_outbound_api import _profile
    return phone_locked(lead, user, api_enabled=_profile(user) is not None)


def record_verified(db, lead_id, provider_id):
    """Durable, lead-wide latch; caller must have persisted provider evidence."""
    db["leads"].update_one({"_id": lead_id}, {"$set": {
        "first_contact_phone_access.verified": True,
        "first_contact_phone_access.provider_message_id": str(provider_id),
    }})


def release_contingency(db, lead, reason):
    """Only call after an authenticated, server-observed failure."""
    cid = cycle_id(lead)
    if not cid:
        return False
    result = db["leads"].update_one(
        {"_id": lead["_id"], "lifecycle.current_assignment_cycle_id": cid},
        {"$set": {"first_contact_phone_access.contingency": {
            "cycle_id": cid, "reason": str(reason), "at": datetime.now(timezone.utc),
        }}},
    )
    return bool(result.matched_count)
