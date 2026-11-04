"""Optional, explicitly gated Render bootstrap for a reviewed small campaign.

Read-only preview by default. The production environment is the only place
allowed to turn preview into a plan or approve it. No provider call is ever
made from this module.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from .price_drop_campaign_real import (
    APPROVED_ID_FLAG,
    CampaignBlocked,
    approve_campaign,
    persist_plan,
    preview_plan,
)

MODE = "PRICE_DROP_REAL_PILOT_BOOTSTRAP_MODE"
PLAN_JSON = "PRICE_DROP_REAL_PILOT_PLAN_JSON"
REVIEWER = "PRICE_DROP_REAL_PILOT_REVIEWER"
_MODES = {"preview", "prepare", "approve"}


def run_bootstrap(db: Any, *, env: Mapping[str, str] | None = None) -> dict:
    """One finite startup operation, never a send or recurring job."""
    settings = env if env is not None else os.environ
    mode = str(settings.get(MODE) or "off").strip().lower()
    if mode not in _MODES:
        return {"status": "DISABLED"}
    approved_id = str(settings.get(APPROVED_ID_FLAG) or "").strip()
    reviewer = str(settings.get(REVIEWER) or "").strip()
    if not approved_id:
        raise CampaignBlocked("approved_campaign_id_required")
    if not reviewer or len(reviewer) > 80:
        raise CampaignBlocked("reviewer_required")
    if mode == "approve":
        result = approve_campaign(db, approved_id, operator=reviewer)
        return {"status": result["status"],
                "campaign_id": approved_id,
                "recipients": result["recipients"]}
    raw_json = str(settings.get(PLAN_JSON) or "")
    if not raw_json or len(raw_json) > 12000:
        raise CampaignBlocked("reviewed_plan_json_required")
    try:
        payload = json.loads(raw_json)
    except ValueError as exc:
        raise CampaignBlocked("plan_json_invalid") from exc
    if not isinstance(payload, dict):
        raise CampaignBlocked("plan_json_not_object")
    if str(payload.get("campaign_id") or "") != approved_id:
        raise CampaignBlocked("approved_campaign_id_mismatch")
    if str(payload.get("reviewed_by") or "") != reviewer:
        raise CampaignBlocked("plan_reviewer_mismatch")
    if mode == "preview":
        preview = preview_plan(db, payload)
        # Never return/log a full phone or the private recipient data.
        return {
            "status": "READY" if preview["ready_to_plan"] else "BLOCKED",
            "campaign_id": approved_id,
            "property_code": preview["property_code"],
            "approved_count": len(preview["approved"]),
            "slots": [
                {"slot": row["slot"],
                 "phone_last4": row["phone_last4"],
                 "scheduled_at": row["scheduled_at"]}
                for row in preview["approved"]
            ],
            "blocked": preview["blocked"],
        }
    result = persist_plan(db, payload)
    return {
        "status": result["status"],
        "campaign_id": approved_id,
        "recipients": result["recipients"],
        "first_slot": result["first_slot"],
        "gap_minutes": result["gap_minutes"],
        "sent": 0,
    }
