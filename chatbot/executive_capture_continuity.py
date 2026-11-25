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


def _provider_connected_epoch(rows, *, session_id, anchor, now):
    """A bounded log page must cover the anchor, without a later interruption."""
    if not isinstance(rows, list) or not rows:
        return None
    parsed = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("whatsapp_session_id")) != str(session_id):
            return None
        try:
            at = datetime.fromisoformat(str(row["occurred_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            return None
        if at.tzinfo is None or at > now:
            return None
        parsed.append((at, row.get("status"), row.get("event_type")))
    parsed.sort(reverse=True, key=lambda x: x[0])
    for at, status, event in parsed:
        if status != "connected":
            return None
        if at <= anchor:
            return at
        if event != "status_change":
            return None
    return None


def recover_provider_continuity(db, *, session_id, owner_id, anchor, now, get=None):
    """Bootstrap missing session metadata using authenticated provider evidence.

    Never overwrite an observed disconnect, never backdate a current status,
    and never assert that provider logs guarantee complete message replay.
    The caller requires a canonical signed webhook echo for the lead anchor.
    """
    import hmac
    from urllib.parse import urlparse
    import requests
    from config import Config
    from pymongo.errors import DuplicateKeyError
    from .executive_whatsapp_audit import _session

    import logging
    sid = str(session_id)
    logger = logging.getLogger(__name__)

    def blocked(reason):
        # Enumerated operational reason only: never log provider payloads,
        # credentials, customer identities or message content.
        logger.warning("[EXEC_SILENCE_CONTINUITY] session=%s reason=%s", sid, reason)
        return None

    existing = db[COLLECTION].find_one({"_id": sid})
    if existing:
        return continuity_since(db, session_id=sid, owner_id=owner_id)
    session = _session(db, sid)
    if not session or session.get("user_id") != str(owner_id):
        return blocked("session_identity_unverified")
    if not session.get("capture_enabled"):
        return blocked("capture_disabled")
    pat = getattr(Config, "WASENDER_PERSONAL_ACCESS_TOKEN", None)
    if not pat:
        return blocked("personal_access_token_missing")
    base = Config.WASENDER_BASE_URL.rstrip("/") + "/whatsapp-sessions/" + sid
    headers = {"Authorization": "Bearer " + pat}
    fetch = get or requests.get
    try:
        response = fetch(base, headers=headers, timeout=5)
        response.raise_for_status()
        envelope = response.json()
        detail = envelope.get("data") or {}
        events = detail.get("webhook_events")
        secret = detail.get("webhook_secret")
        if envelope.get("success") is not True or str(detail.get("id")) != sid:
            return blocked("provider_details_unverified")
        if detail.get("status") != "connected":
            return blocked("provider_session_not_connected")
        if detail.get("webhook_enabled") is not True:
            return blocked("provider_webhook_disabled")
        if not isinstance(events, list) or not ({"messages.upsert", "messages.received"} & set(events)):
            return blocked("provider_inbound_events_missing")
        if urlparse(str(detail.get("webhook_url") or "")).path.rstrip("/") != "/webhook/ejecutivos/" + sid:
            return blocked("provider_webhook_path_mismatch")
        if not isinstance(secret, str) or not hmac.compare_digest(secret, session["secret"]):
            return blocked("provider_webhook_secret_mismatch")
        response = fetch(base + "/session-logs", headers=headers,
                         params={"page": 1, "per_page": 100}, timeout=5)
        # The provider documents a session API key for this log endpoint.
        # Fall back only on explicit authentication rejection, never on a
        # timeout or a rate limit, using the same verified session.
        if response.status_code in (401, 403):
            response = fetch(base + "/session-logs",
                             headers={"Authorization": "Bearer " + session["token"]},
                             params={"page": 1, "per_page": 100}, timeout=5)
        response.raise_for_status()
        envelope = response.json()
        page = envelope.get("data") or {}
        if envelope.get("success") is not True or page.get("current_page") != 1:
            return blocked("provider_log_page_unverified")
        epoch = _provider_connected_epoch(page.get("data"), session_id=sid, anchor=anchor, now=now)
        if epoch is None:
            return blocked("provider_log_history_incomplete_or_interrupted")
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        return blocked("provider_http_" + str(code))
    except requests.RequestException:
        return blocked("provider_request_failed")
    except (ValueError, TypeError, AttributeError, KeyError):
        return blocked("provider_response_invalid")
    # Do not overwrite a newer signed status event during the provider calls.
    try:
        db[COLLECTION].update_one({"_id": sid}, {"$setOnInsert": {
            "owner_id": str(owner_id), "status": "connected",
            "connected_since": epoch, "status_observed_at": now,
            "evidence_source": "provider_session_logs",
            "provider_webhook_configuration_checked_at": now,
        }}, upsert=True)
    except DuplicateKeyError:
        pass
    return continuity_since(db, session_id=sid, owner_id=owner_id)
