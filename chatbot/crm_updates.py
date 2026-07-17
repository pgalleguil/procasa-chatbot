"""Atomic change token used by the CRM leads list polling endpoint."""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from pymongo import ReturnDocument

from .constants import CHILE_TZ


logger = logging.getLogger(__name__)

CRM_RUNTIME_COLLECTION = "crm_runtime_state"
CRM_LEADS_VERSION_ID = "crm_leads_list"


def bump_crm_leads_version(
    db,
    reason: str = "lead_updated",
    phone: Optional[str] = None,
) -> int:
    """Increment the global CRM list version without failing the business write."""
    try:
        now = datetime.now(CHILE_TZ)
        update: Dict[str, Any] = {
            "$inc": {"version": 1},
            "$set": {
                "updated_at": now,
                "reason": str(reason or "lead_updated")[:80],
            },
            "$setOnInsert": {"created_at": now},
        }
        if phone:
            update["$set"]["phone"] = str(phone)

        state = db[CRM_RUNTIME_COLLECTION].find_one_and_update(
            {"_id": CRM_LEADS_VERSION_ID},
            update,
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int((state or {}).get("version", 0))
    except Exception as exc:
        logger.warning("[CRM_UPDATES] No se pudo incrementar la version: %s", exc)
        return 0


def get_crm_leads_version(db) -> int:
    """Read the current version using one indexed lookup by ``_id``."""
    state = db[CRM_RUNTIME_COLLECTION].find_one(
        {"_id": CRM_LEADS_VERSION_ID},
        {"version": 1},
    )
    return int((state or {}).get("version", 0))


async def get_crm_leads_version_async(db) -> int:
    """Async equivalent used by FastAPI endpoints."""
    state = await db[CRM_RUNTIME_COLLECTION].find_one(
        {"_id": CRM_LEADS_VERSION_ID},
        {"version": 1},
    )
    return int((state or {}).get("version", 0))
