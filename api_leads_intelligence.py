"""Read-only helpers for the live chat detail view."""

import logging

from chatbot.storage import get_db

logger = logging.getLogger(__name__)


def get_specific_lead_chat(phone):
    """Return the conversation payload for the current CRM chat-detail route."""
    try:
        doc = get_db()["leads"].find_one({"phone": phone})
        if not doc:
            return None
        return {
            "phone": doc.get("phone"),
            "prospecto": doc.get("prospecto", {}),
            "messages": doc.get("messages", []),
            "bi_analytics_global": doc.get("bi_analytics_global", {}),
        }
    except Exception as exc:
        logger.error("Error obteniendo chat %s: %s", phone, exc)
        return None
