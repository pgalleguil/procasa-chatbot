"""Session capture continuity markers, not a guarantee of historical replay.

Only call from the already authenticated per-session webhook. Persist session
status metadata only, never a chat, token, raw payload, phone or customer data.
An unknown or interrupted epoch cannot authorize an old conversation.
"""
from datetime import datetime, timezone

COLLECTION = "crm_exec_whatsapp_capture_continuity"
KNOWN = {"connected", "connecting", "disconnected", "logged_out",
         "expired", "need_scan", "need_passkey"}


def record_status(db, *, session_id: str, owner_id: str, status: str,
                  now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or not session_id or not owner_id:
        raise ValueError("invalid_status_identity")
    status = status if status in KNOWN else "unknown"
    # Every event invalidates older anchors, including duplicate connected
    # notifications. Conservative on replay / ordering; no fake recovery.
    db[COLLECTION].update_one(
        {"_id": str(session_id)},
        {"$set": {"owner_id": str(owner_id), "status": status,
                  "status_observed_at": now,
                  "connected_since": now if status == "connected" else None}},
        upsert=True,
    )


def continuity_since(db, *, session_id: str, owner_id: str):
    row = db[COLLECTION].find_one({"_id": str(session_id),
                                  "owner_id": str(owner_id), "status": "connected"})
    if not row:
        return None
    value = row.get("connected_since")
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
