"""
Canonical CRM delivery functions.

Single source of truth for:
- resolve_executive_user: find the executive user from various ID formats
- validate_executive_recipient: validate phone before sending
- send_whatsapp_to_executive: send WhatsApp to an executive
"""
import logging
logger = logging.getLogger(__name__)

PLACEHOLDER_PHONES = frozenset({"+56900000000", "56900000000"})


def resolve_executive_user(db, user_id: str) -> dict | None:
    """Resolve an executive user from the usuarios collection.

    Tries in order:
    1. ObjectId from hex string
    2. Raw string _id (legacy)
    3. Exact nombre match (logged as fallback)
    Returns the user dict or None.
    """
    from bson import ObjectId
    from bson.errors import InvalidId

    if not user_id:
        return None

    # 1. Try as ObjectId hex string
    if len(user_id) == 24:
        try:
            oid = ObjectId(user_id)
            user = db["usuarios"].find_one({"_id": oid})
            if user:
                return user
        except InvalidId:
            pass

    # 2. Try as raw string _id (legacy)
    user = db["usuarios"].find_one({"_id": user_id})
    if user:
        return user

    # 3. Fallback: exact nombre match
    user = db["usuarios"].find_one({"nombre": user_id})
    if user:
        logger.info("[CRM_DELIVERY] recipient_resolution_mode=name_fallback for %s", user_id[:20])
        return user

    logger.warning("[CRM_DELIVERY] recipient not resolved: %s", str(user_id)[:24])
    return None


def get_executive_phone(user: dict) -> str | None:
    """Get the canonical phone from a user dict."""
    phone = str(user.get("telefono") or user.get("tel") or user.get("movil") or "").strip()
    if not phone or phone in PLACEHOLDER_PHONES:
        return None
    return phone


def validate_executive_recipient(phone: str | None) -> str | None:
    """Validate and normalize an executive phone number.

    Returns normalized phone or None if invalid.
    Never returns a placeholder or fictitious number.
    """
    if not phone:
        return None
    phone = phone.strip()
    if phone in PLACEHOLDER_PHONES:
        return None
    # Extract digits
    digits = "".join(filter(str.isdigit, phone))
    if len(digits) < 9:
        return None
    # Must be a Chilean mobile (+569...)
    if digits.startswith("569") and len(digits) == 12:
        return "+" + digits
    if digits.startswith("56") and len(digits) == 11:
        return "+" + digits
    if digits.startswith("9") and len(digits) == 9:
        return "+56" + digits
    # Already has + or other country code — accept if valid
    if phone.startswith("+") and len(digits) >= 10:
        return "+" + digits
    return None


def send_whatsapp_to_executive(db, *, user, message, notification_id=None, pipeline="unknown"):
    """Send a WhatsApp message to an executive.

    Validates the recipient before calling the provider.
    Never sends to placeholder or invalid phones.
    """
    phone = get_executive_phone(user)
    if not phone:
        logger.warning("[CRM_DELIVERY] no_phone pipeline=%s user=%s", pipeline, str(user.get("_id"))[:12])
        return {"success": False, "provider_message_id": None, "http_status": None,
                "error": "executive_phone_missing_or_invalid", "provider_called": False}

    validated = validate_executive_recipient(phone)
    if not validated:
        logger.warning("[CRM_DELIVERY] invalid_phone pipeline=%s user=%s phone_end=%s",
                       pipeline, str(user.get("_id"))[:12], phone[-4:])
        return {"success": False, "provider_message_id": None, "http_status": None,
                "error": "executive_phone_invalid", "provider_called": False}

    from chatbot.whatsapp_client import send_whatsapp_message_detailed
    import asyncio

    logger.info("[CRM_DELIVERY] sending pipeline=%s notif=%s user=%s phone_end=%s msg_len=%d",
                pipeline, str(notification_id)[:12] if notification_id else "?",
                str(user.get("_id"))[:12], validated[-4:], len(message))

    result = asyncio.run(send_whatsapp_message_detailed(validated, message))

    success = bool(result.get("success"))
    provider_id = result.get("provider_message_id")
    http_status = result.get("http_status")

    if success and provider_id:
        logger.info("[CRM_DELIVERY] sent pipeline=%s notif=%s provider=%s",
                    pipeline, str(notification_id)[:12] if notification_id else "?", provider_id)

    return result
