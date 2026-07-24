"""
Canonical CRM delivery functions.

Single source of truth for:
- resolve_executive_user: find the executive user from various ID formats
- send_whatsapp_to_executive: send WhatsApp to an executive using the known working format
"""
import logging
logger = logging.getLogger(__name__)


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

    # 1. Try as ObjectId hex string (24 chars)
    if len(user_id) == 24:
        try:
            oid = ObjectId(user_id)
            user = db["usuarios"].find_one({"_id": oid})
            if user:
                return user
        except InvalidId:
            pass

    # 2. Try as raw string _id (legacy string IDs)
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


def send_whatsapp_to_executive(db, *, user, message, notification_id=None, pipeline="unknown"):
    """Send a WhatsApp message to an executive using the canonical WASender format.

    Args:
        db: database connection
        user: executive user dict (from resolve_executive_user)
        message: message text to send
        notification_id: optional notification ID for logging
        pipeline: pipeline name for logging (digest, hot, sla)

    Returns:
        dict with success, provider_message_id, http_status
    """
    phone = str(user.get("telefono") or "").strip()
    if not phone:
        logger.warning("[CRM_DELIVERY] no_phone pipeline=%s user=%s", pipeline, str(user.get("_id"))[:12])
        return {"success": False, "provider_message_id": None, "http_status": None, "error": "no_phone"}

    from chatbot.whatsapp_client import send_whatsapp_message_detailed
    import asyncio

    # Log context (safe: only last 4 digits)
    logger.info("[CRM_DELIVERY] sending pipeline=%s notif=%s user=%s phone_end=%s msg_len=%d",
                pipeline, str(notification_id)[:12] if notification_id else "?",
                str(user.get("_id"))[:12], phone[-4:], len(message))

    # Use the same WASender function that HOT notifications use
    result = asyncio.run(send_whatsapp_message_detailed(phone, message))

    success = bool(result.get("success"))
    provider_id = result.get("provider_message_id")
    http_status = result.get("http_status")

    if success and provider_id:
        logger.info("[CRM_DELIVERY] sent pipeline=%s notif=%s provider=%s",
                    pipeline, str(notification_id)[:12] if notification_id else "?", provider_id)
    elif http_status in (422, 429):
        logger.warning("[CRM_DELIVERY] provider_error pipeline=%s notif=%s http=%s body=%s",
                       pipeline, str(notification_id)[:12] if notification_id else "?", http_status,
                       str(result.get("response_body", result.get("error", "")))[:200])

    return result
