"""Canonical attribution for scheduled CRM follow-ups.

The task identifier already stored in ``crm_tasks`` is the lifecycle identity.
This module adds signed, opaque routing and append-only funnel events without
changing the existing management ledgers.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from statistics import median
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from pymongo.errors import DuplicateKeyError


TRACKING_VERSION = "followup_tracking_v1"
EVENT_COLLECTION = "followup_events"
DETAIL_OPEN_UNIQUES_COLLECTION = "captacion_detail_open_uniques"
EVENT_TYPES = frozenset({
    "reminder_scheduled",
    "reminder_sent",
    "reminder_clicked",
    "lead_opened",
    "followup_management_created",
})
TOKEN_TTL = timedelta(days=90)
DETAIL_OPEN_DEDUPE_WINDOW = timedelta(minutes=5)
DETAIL_OPEN_SOURCES = frozenset({"captacion_list", "whatsapp_followup", "direct"})
TERMINAL_UNATTRIBUTABLE_STATUSES = frozenset({"failed_terminal", "cancelled"})
_INDEXES_READY_DB_IDS: set[int] = set()


class FollowupTokenError(ValueError):
    """The token or its referenced task cannot be used for attribution."""


def ensure_followup_indexes(db) -> None:
    global _INDEXES_READY_DB_IDS
    db_key = id(getattr(db, "client", db))
    if db_key in _INDEXES_READY_DB_IDS:
        return
    event_collection = db[EVENT_COLLECTION]
    create_index = getattr(event_collection, "create_index", None)
    if create_index:
        create_index([("task_id", 1), ("event_type", 1)], name="followup_task_event")
        create_index([("property_id", 1), ("occurred_at", 1)], name="followup_property_time")
        create_index(
            [("event_type", 1), ("executive_id", 1), ("property_id", 1), ("occurred_at", 1)],
            name="captacion_detail_open_query",
        )
    task_collection = db["crm_tasks"]
    task_create_index = getattr(task_collection, "create_index", None)
    if task_create_index:
        task_create_index("task_id", name="crm_task_lifecycle_id")
    unique_collection = db[DETAIL_OPEN_UNIQUES_COLLECTION]
    unique_create_index = getattr(unique_collection, "create_index", None)
    if unique_create_index:
        unique_create_index(
            [("executive_id", 1), ("property_id", 1)],
            unique=True,
            name="captacion_detail_open_actor_property",
        )
    _INDEXES_READY_DB_IDS.add(db_key)


def _secret() -> bytes:
    from config import Config

    return str(Config.SECRET_KEY).encode("utf-8")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(body: str) -> str:
    return _encode(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())


def issue_followup_token(task_id: Any, *, expires_at: Any = None, now: Any = None) -> str:
    task_value = str(task_id or "").strip()
    if not task_value:
        raise FollowupTokenError("followup_task_id_missing")
    current = now or datetime.now(timezone.utc)
    if isinstance(current, datetime):
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current_epoch = current.timestamp()
    else:
        current_epoch = time.time()
    expiry = expires_at
    if isinstance(expiry, datetime):
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        expiry_epoch = max(expiry.timestamp(), current_epoch) + TOKEN_TTL.total_seconds()
    else:
        expiry_epoch = current_epoch + TOKEN_TTL.total_seconds()
    payload = {"v": 1, "task_id": task_value, "exp": int(expiry_epoch)}
    body = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_sign(body)}"


def verify_followup_token(token: str, *, now: Any = None) -> dict[str, Any]:
    raw = str(token or "").strip()
    try:
        body, signature = raw.split(".", 1)
        expected = _sign(body)
        if not hmac.compare_digest(signature, expected):
            raise FollowupTokenError("followup_token_invalid")
        payload = json.loads(_decode(body).decode("utf-8"))
        if payload.get("v") != 1 or not str(payload.get("task_id") or "").strip():
            raise FollowupTokenError("followup_token_invalid")
        current = now.timestamp() if isinstance(now, datetime) else time.time()
        if int(payload.get("exp") or 0) < int(current):
            raise FollowupTokenError("followup_token_expired")
        return payload
    except FollowupTokenError:
        raise
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        raise FollowupTokenError("followup_token_invalid")


def is_tracked_task(task: Mapping[str, Any] | None) -> bool:
    return bool(task and task.get("followup_tracking_version") == TRACKING_VERSION)


def task_query(task_id: Any) -> dict[str, Any]:
    return {"task_id": str(task_id), "followup_tracking_version": TRACKING_VERSION}


def find_tracked_task(db, task_id: Any) -> dict[str, Any] | None:
    return db["crm_tasks"].find_one(task_query(task_id))


def task_target_id(task: Mapping[str, Any]) -> str:
    return str(task.get("recipient_user_id") or task.get("target_user_id") or "").strip()


def task_entity_id(task: Mapping[str, Any]) -> str:
    return str(task.get("obj_id") or task.get("lead_id") or "").strip()


def task_followup_cycle_id(task: Mapping[str, Any]) -> str | None:
    return str(
        task.get("followup_cycle_id")
        or task.get("follow_up_cycle_id")
        or task.get("assignment_cycle_id")
        or ""
    ).strip() or None


def build_followup_open_url(task: Mapping[str, Any], *, base_url: str | None = None) -> str | None:
    if not is_tracked_task(task) or not task.get("task_id"):
        return None
    from config import Config
    from urllib.parse import quote

    token = issue_followup_token(task["task_id"], expires_at=task.get("execute_at"))
    base = str(base_url or Config.CRM_BASE_URL).rstrip("/")
    return f"{base}/followup/open/{quote(token, safe='')}"


def _event_id(task_id: str, event_type: str, management_event_id: str | None = None) -> str:
    if event_type == "followup_management_created" and management_event_id:
        return f"followup:{task_id}:{event_type}:{management_event_id}"
    return f"followup:{task_id}:{event_type}:{uuid.uuid4()}"


def _first_timestamp_update(task: Mapping[str, Any], field: str, value: datetime) -> None:
    # Kept as a helper for callers that want the canonical field mapping.
    task[field] = value


def _normalise_event_time(value: Any = None) -> datetime:
    when = value or datetime.now(timezone.utc)
    if not isinstance(when, datetime):
        raise FollowupTokenError("followup_timestamp_invalid")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _mark_unique_detail_open(db, *, executive_id: str | None, property_id: str, opened_at: datetime) -> bool:
    """Atomically mark the first opening outside the five-minute window."""
    if not executive_id or not property_id:
        return True
    threshold = opened_at - DETAIL_OPEN_DEDUPE_WINDOW
    try:
        result = db[DETAIL_OPEN_UNIQUES_COLLECTION].update_one(
            {
                "executive_id": executive_id,
                "property_id": property_id,
                "$or": [
                    {"last_opened_at": {"$exists": False}},
                    {"last_opened_at": {"$lte": threshold}},
                ],
            },
            {
                "$set": {"last_opened_at": opened_at},
                "$setOnInsert": {
                    "executive_id": executive_id,
                    "property_id": property_id,
                    "created_at": opened_at,
                },
            },
            upsert=True,
        )
        return bool(result.upserted_id is not None or result.modified_count)
    except DuplicateKeyError:
        # Another request won the same actor/property insert concurrently.
        return False


def record_detail_open(
    db,
    *,
    entity_id: Any,
    executive_id: Any,
    source: str,
    task: Mapping[str, Any] | None = None,
    followup_cycle_id: Any = None,
    opened_at: Any = None,
) -> dict[str, Any]:
    """Record every authenticated detail opening and flag unique openings.

    Raw ``lead_opened`` events are append-only. ``is_unique_open`` is the KPI
    view: the same executive/property pair counts once per five-minute window.
    Follow-up fields remain null unless a validated task is supplied.
    """
    ensure_followup_indexes(db)
    when = _normalise_event_time(opened_at)
    property_id = str(entity_id or "").strip()
    executive = str(executive_id or "").strip() or None
    tracked_task_id = str(task.get("task_id") or "").strip() if task else None
    if task and not is_tracked_task(task):
        raise FollowupTokenError("legacy_unattributed")
    if task:
        source = "whatsapp_followup"
        cycle_id = str(followup_cycle_id or task_followup_cycle_id(task) or "").strip() or None
    else:
        source = source if source in {"captacion_list", "direct"} else "direct"
        cycle_id = None
    event = {
        "_id": f"captacion_detail_opened:{uuid.uuid4()}",
        "event_id": str(uuid.uuid4()),
        "event_type": "lead_opened",
        "task_id": tracked_task_id,
        "followup_task_id": tracked_task_id,
        "followup_cycle_id": cycle_id,
        "property_id": property_id or None,
        "lead_id": str(task.get("lead_id") or "") or None if task else None,
        "executive_id": executive,
        "actor_user_id": executive,
        "opened_at": when,
        "occurred_at": when,
        "source": source,
        "attribution_status": "attributed" if task else "unattributed",
        "is_unique_open": False,
        "created_at": when,
    }
    db[EVENT_COLLECTION].insert_one(event)
    is_unique = _mark_unique_detail_open(
        db, executive_id=executive, property_id=property_id, opened_at=when,
    )
    db[EVENT_COLLECTION].update_one(
        {"_id": event["_id"]},
        {"$set": {"is_unique_open": is_unique, "unique_opened_at": when if is_unique else None}},
    )
    if task and tracked_task_id:
        db["crm_tasks"].update_one(
            {"_id": task.get("_id"), "task_id": tracked_task_id, "opened_at": {"$exists": False}},
            {"$set": {"opened_at": when, "attribution_status": "attributed"}},
        )
    event["is_unique_open"] = is_unique
    event["unique_opened_at"] = when if is_unique else None
    return event


def record_captacion_detail_open(
    db,
    *,
    property_id: Any,
    executive_id: Any,
    source: str = "direct",
    followup_task: Mapping[str, Any] | None = None,
    followup_cycle_id: Any = None,
    opened_at: Any = None,
) -> dict[str, Any]:
    return record_detail_open(
        db,
        entity_id=property_id,
        executive_id=executive_id,
        source=source,
        task=followup_task,
        followup_cycle_id=followup_cycle_id,
        opened_at=opened_at,
    )


def summarize_captacion_detail_opens(db, *, executive_id: Any = None, since: Any = None, until: Any = None) -> dict[str, Any]:
    """Return queryable opening/follow-up counters without adding dashboard UI."""
    query: dict[str, Any] = {"event_type": "lead_opened", "property_id": {"$ne": None}}
    if executive_id:
        query["executive_id"] = str(executive_id)
    if since or until:
        query["occurred_at"] = {}
        if since:
            query["occurred_at"]["$gte"] = _normalise_event_time(since)
        if until:
            query["occurred_at"]["$lt"] = _normalise_event_time(until)
    opens = list(db[EVENT_COLLECTION].find(query).sort("occurred_at", 1))
    unique_opens = [event for event in opens if event.get("is_unique_open")]
    reminder_opens = [event for event in opens if event.get("source") == "whatsapp_followup"]
    manual_opens = [event for event in opens if event.get("source") == "captacion_list"]
    direct_opens = [event for event in opens if event.get("source") == "direct"]
    unique_reminder_opens = [event for event in reminder_opens if event.get("is_unique_open")]
    management_by_task: dict[str, datetime] = {}
    for event in db[EVENT_COLLECTION].find({"event_type": "followup_management_created"}):
        task_id = str(event.get("followup_task_id") or event.get("task_id") or "").strip()
        occurred = event.get("occurred_at")
        if task_id and isinstance(occurred, datetime):
            management_by_task[task_id] = min(management_by_task.get(task_id, occurred), occurred)
    opened_task_ids = {str(event.get("followup_task_id") or event.get("task_id")) for event in reminder_opens}
    clicked_task_ids = {
        str(event.get("task_id"))
        for event in db[EVENT_COLLECTION].find({"event_type": "reminder_clicked"})
        if event.get("task_id")
    }
    delays = []
    opens_without_management = 0
    opens_with_management = 0
    within = {"1h": 0, "3h": 0, "24h": 0}
    for event in unique_reminder_opens:
        task_id = str(event.get("followup_task_id") or event.get("task_id") or "").strip()
        opened = event.get("opened_at") or event.get("occurred_at")
        managed = management_by_task.get(task_id)
        if not managed or not isinstance(opened, datetime) or managed < opened:
            opens_without_management += 1
            continue
        opens_with_management += 1
        delay = (managed - opened).total_seconds()
        delays.append(delay)
        if delay <= 3600:
            within["1h"] += 1
        if delay <= 10800:
            within["3h"] += 1
        if delay <= 86400:
            within["24h"] += 1
    return {
        "properties_opened": len(opens),
        "properties_unique_opened": len({event.get("property_id") for event in unique_opens if event.get("property_id")}),
        "unique_openings": len(unique_opens),
        "manual_openings": len(manual_opens) + len(direct_opens),
        "captacion_list_openings": len(manual_opens),
        "direct_openings": len(direct_opens),
        "reminder_openings": len(reminder_opens),
        "unique_reminder_openings": len(unique_reminder_opens),
        "reminder_clicks_without_open": len(clicked_task_ids - opened_task_ids),
        "openings_without_management": opens_without_management,
        "openings_with_management": opens_with_management,
        "median_open_to_management_seconds": median(delays) if delays else None,
        "open_to_management_within": within,
    }


def record_followup_event(
    db,
    *,
    task: Mapping[str, Any],
    event_type: str,
    occurred_at: Any = None,
    source: str = "followup",
    actor_user_id: Any = None,
    management_event_id: Any = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise FollowupTokenError("followup_event_type_invalid")
    if not is_tracked_task(task):
        raise FollowupTokenError("legacy_unattributed")
    ensure_followup_indexes(db)
    task_id = str(task.get("task_id") or "").strip()
    when = _normalise_event_time(occurred_at)
    followup_cycle_id = task_followup_cycle_id(task)
    event = {
        "_id": _event_id(task_id, event_type, str(management_event_id or "") or None),
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "task_id": task_id,
        "followup_task_id": task_id,
        "followup_cycle_id": followup_cycle_id,
        "lead_id": str(task.get("lead_id") or "") or None,
        "property_id": str(task.get("obj_id") or "") or None,
        "executive_id": task_target_id(task) or None,
        "actor_user_id": str(actor_user_id or "") or None,
        "scheduled_at": task.get("scheduled_at") or task.get("execute_at"),
        "sent_at": task.get("sent_at") or task.get("delivered_at") or task.get("notified_at"),
        "occurred_at": when,
        "opened_at": when if event_type == "lead_opened" else None,
        "source": source,
        "attribution_status": "attributed",
        "created_at": when,
    }
    if extra:
        event.update(dict(extra))
    try:
        db[EVENT_COLLECTION].insert_one(event)
    except DuplicateKeyError:
        existing = db[EVENT_COLLECTION].find_one({"_id": event["_id"]})
        if existing:
            return existing
        raise

    field_by_event = {
        "reminder_scheduled": None,
        "reminder_sent": "sent_at",
        "reminder_clicked": "clicked_at",
        "lead_opened": "opened_at",
        "followup_management_created": "followup_management_at",
    }
    task_id_filter = {"_id": task.get("_id"), "task_id": task_id}
    field = field_by_event[event_type]
    if field:
        db["crm_tasks"].update_one(
            {**task_id_filter, field: {"$exists": False}},
            {"$set": {field: when, "attribution_status": "attributed"}},
        )
    return event


def record_followup_open(db, *, token: str, entity_id: Any, actor_user_id: Any = None) -> dict[str, Any]:
    payload = verify_followup_token(token)
    task = find_tracked_task(db, payload["task_id"])
    if not task:
        raise FollowupTokenError("followup_task_not_found")
    if task.get("status") in TERMINAL_UNATTRIBUTABLE_STATUSES or task.get("resolution") in {"superseded", "superseded_duplicate"}:
        raise FollowupTokenError("followup_task_unavailable")
    if str(entity_id) != task_entity_id(task):
        raise FollowupTokenError("followup_entity_mismatch")
    expected_actor = task_target_id(task)
    if actor_user_id and expected_actor and str(actor_user_id) != expected_actor:
        raise FollowupTokenError("followup_actor_mismatch")
    return record_detail_open(
        db,
        entity_id=entity_id,
        executive_id=actor_user_id or expected_actor,
        source="whatsapp_followup",
        task=task,
    )


def record_followup_management(
    db,
    *,
    token: str,
    entity_id: Any,
    executive_id: Any,
    management_event_id: Any,
    occurred_at: Any = None,
    followup_cycle_id: Any = None,
) -> dict[str, Any]:
    payload = verify_followup_token(token)
    task = find_tracked_task(db, payload["task_id"])
    if not task:
        raise FollowupTokenError("followup_task_not_found")
    if task.get("status") in TERMINAL_UNATTRIBUTABLE_STATUSES or task.get("resolution") in {"superseded", "superseded_duplicate"}:
        raise FollowupTokenError("followup_task_unavailable")
    if str(entity_id) != task_entity_id(task):
        raise FollowupTokenError("followup_entity_mismatch")
    expected_actor = task_target_id(task)
    if expected_actor and str(executive_id) != expected_actor:
        raise FollowupTokenError("followup_actor_mismatch")
    when = occurred_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    scheduled = task.get("scheduled_at") or task.get("execute_at")
    if isinstance(scheduled, datetime):
        scheduled = scheduled if scheduled.tzinfo else scheduled.replace(tzinfo=timezone.utc)
        if when <= scheduled:
            raise FollowupTokenError("followup_management_before_schedule")
    extra = {
        "followup_task_id": str(task["task_id"]),
        "management_event_id": str(management_event_id),
        "followup_cycle_id": str(followup_cycle_id or task_followup_cycle_id(task) or "") or None,
    }
    return record_followup_event(
        db,
        task=task,
        event_type="followup_management_created",
        occurred_at=when,
        source="captacion_followup" if task.get("lead_type") == "captacion" else "crm_followup",
        actor_user_id=executive_id,
        management_event_id=str(management_event_id),
        extra=extra,
    )
