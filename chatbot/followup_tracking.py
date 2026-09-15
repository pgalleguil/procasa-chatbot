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
import logging
from statistics import median
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from pymongo.errors import DuplicateKeyError


logger = logging.getLogger(__name__)

TOKEN_VERSION = 2
TRACKING_VERSION = "followup_tracking_v2"
LEGACY_TRACKING_VERSION = "followup_tracking_v1"
TRACKING_VERSIONS = frozenset({TRACKING_VERSION, LEGACY_TRACKING_VERSION})
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


class FollowupConfigurationError(RuntimeError):
    """The process cannot safely issue or validate a signed follow-up token."""


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
        # Do not collide with historical v1/legacy duplicates. New v2 tasks
        # are protected by a partial unique key instead.
        task_create_index(
            [("idempotency_key", 1)],
            unique=True,
            partialFilterExpression={
                "followup_tracking_version": TRACKING_VERSION,
                "idempotency_key": {"$exists": True},
            },
            name="crm_task_captacion_v2_idempotency",
        )
    unique_collection = db[DETAIL_OPEN_UNIQUES_COLLECTION]
    unique_create_index = getattr(unique_collection, "create_index", None)
    if unique_create_index:
        unique_create_index(
            [("executive_id", 1), ("property_id", 1)],
            unique=True,
            name="captacion_detail_open_actor_property",
        )
    _INDEXES_READY_DB_IDS.add(db_key)


def _secret(version: int) -> bytes:
    from config import Config

    try:
        return Config.require_followup_token_secret(version=version).encode("utf-8")
    except RuntimeError as exc:
        raise FollowupConfigurationError("followup_token_configuration_invalid") from exc


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(body: str, *, version: int) -> str:
    return _encode(hmac.new(_secret(version), body.encode("ascii"), hashlib.sha256).digest())


def _issue_token_for_version(
    task_id: Any,
    *,
    expires_at: Any = None,
    now: Any = None,
    version: int = TOKEN_VERSION,
) -> str:
    task_value = str(task_id or "").strip()
    if not task_value:
        raise FollowupTokenError("followup_task_id_missing")
    try:
        version = int(version)
    except (TypeError, ValueError):
        raise FollowupTokenError("followup_token_invalid")
    if version not in {1, TOKEN_VERSION}:
        raise FollowupTokenError("followup_token_invalid")
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
        # The task's scheduled execution is the lifetime anchor.  A delayed
        # worker must not silently extend the link from its delivery time.
        expiry_epoch = expiry.timestamp() + TOKEN_TTL.total_seconds()
    else:
        expiry_epoch = current_epoch + TOKEN_TTL.total_seconds()
    payload = {"v": version, "task_id": task_value, "exp": int(expiry_epoch)}
    body = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    try:
        signature = _sign(body, version=version)
    except FollowupConfigurationError:
        logger.error(
            "[FOLLOWUP] token_issue_failed task_id=%s token_version=%s",
            task_value,
            version,
        )
        raise
    return f"{body}.{signature}"


def issue_followup_token(task_id: Any, *, expires_at: Any = None, now: Any = None) -> str:
    """Issue a new v2 token. New code cannot accidentally mint legacy v1."""
    return _issue_token_for_version(task_id, expires_at=expires_at, now=now, version=TOKEN_VERSION)


def _issue_legacy_followup_token(task_id: Any, *, expires_at: Any = None, now: Any = None) -> str:
    """Issue v1 only for an explicitly enabled legacy delivery path."""
    return _issue_token_for_version(task_id, expires_at=expires_at, now=now, version=1)


def verify_followup_token(token: str, *, now: Any = None) -> dict[str, Any]:
    raw = str(token or "").strip()
    try:
        body, signature = raw.split(".", 1)
        payload = json.loads(_decode(body).decode("utf-8"))
        if not isinstance(payload, dict):
            raise FollowupTokenError("followup_token_invalid")
        version = int(payload.get("v") or 0)
        if version not in {1, TOKEN_VERSION}:
            raise FollowupTokenError("followup_token_invalid")
        expected = _sign(body, version=version)
        if not hmac.compare_digest(signature, expected):
            logger.warning(
                "[FOLLOWUP] token_validation_failed token_version=%s reason=signature_mismatch",
                version,
            )
            raise FollowupTokenError("followup_token_invalid")
        if not str(payload.get("task_id") or "").strip() or not int(payload.get("exp") or 0):
            logger.warning(
                "[FOLLOWUP] token_validation_failed token_version=%s reason=payload_invalid",
                version,
            )
            raise FollowupTokenError("followup_token_invalid")
        current_value = now or datetime.now(timezone.utc)
        if isinstance(current_value, datetime):
            if current_value.tzinfo is None:
                current_value = current_value.replace(tzinfo=timezone.utc)
            current = current_value.timestamp()
        else:
            current = time.time()
        if int(payload["exp"]) < int(current):
            logger.info(
                "[FOLLOWUP] token_expired task_id=%s token_version=%s",
                str(payload.get("task_id")),
                version,
            )
            raise FollowupTokenError("followup_token_expired")
        return payload
    except FollowupConfigurationError:
        logger.error("[FOLLOWUP] token_validation_failed reason=configuration_invalid")
        raise
    except FollowupTokenError:
        raise
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError, OverflowError):
        logger.warning("[FOLLOWUP] token_validation_failed token_version=unknown reason=malformed")
        raise FollowupTokenError("followup_token_invalid")


def is_tracked_task(task: Mapping[str, Any] | None) -> bool:
    return bool(task and task.get("followup_tracking_version") in TRACKING_VERSIONS)


def tracking_version_for_token(version: Any) -> str | None:
    try:
        normalized = int(version)
    except (TypeError, ValueError):
        return None
    return {
        TOKEN_VERSION: TRACKING_VERSION,
        1: LEGACY_TRACKING_VERSION,
    }.get(normalized)


def task_query(task_id: Any, *, token_version: Any = None) -> dict[str, Any]:
    query = {"task_id": str(task_id)}
    tracking_version = tracking_version_for_token(token_version) if token_version is not None else None
    if tracking_version:
        query["followup_tracking_version"] = tracking_version
    else:
        query["followup_tracking_version"] = {"$in": list(TRACKING_VERSIONS)}
    return query


def find_tracked_task(db, task_id: Any, *, token_version: Any = None) -> dict[str, Any] | None:
    if token_version is None:
        # Two explicit lookups preserve compatibility with lightweight test
        # doubles and avoid an unbounded version fallback.
        for version in (TOKEN_VERSION, 1):
            task = db["crm_tasks"].find_one(task_query(task_id, token_version=version))
            if task:
                return task
        return None
    tracking_version = tracking_version_for_token(token_version)
    if not tracking_version:
        return None
    return db["crm_tasks"].find_one(task_query(task_id, token_version=token_version))


def task_target_id(task: Mapping[str, Any]) -> str:
    return str(task.get("recipient_user_id") or task.get("target_user_id") or "").strip()


def task_recipient_id(task: Mapping[str, Any]) -> str:
    """Return the explicit recipient identity, without legacy fallbacks."""
    return str(task.get("recipient_user_id") or "").strip()


def expected_actor_id(task: Mapping[str, Any], token_version: Any) -> str:
    """Resolve the actor allowed by a token version."""
    try:
        version = int(token_version)
    except (TypeError, ValueError):
        version = TOKEN_VERSION
    if version == TOKEN_VERSION:
        return task_recipient_id(task)
    return task_target_id(task)


def task_entity_id(task: Mapping[str, Any]) -> str:
    return str(task.get("obj_id") or task.get("lead_id") or "").strip()


def task_followup_cycle_id(task: Mapping[str, Any]) -> str | None:
    return str(
        task.get("followup_cycle_id")
        or task.get("follow_up_cycle_id")
        or task.get("assignment_cycle_id")
        or ""
    ).strip() or None


def build_followup_open_url(
    task: Mapping[str, Any], *, base_url: str | None = None, emitted_at: Any = None
) -> str | None:
    if not is_tracked_task(task) or not task.get("task_id"):
        return None
    from config import Config
    from urllib.parse import quote

    task_version = 2 if task.get("followup_tracking_version") == TRACKING_VERSION else 1
    issue = issue_followup_token if task_version == TOKEN_VERSION else _issue_legacy_followup_token
    token = issue(task["task_id"], expires_at=task.get("execute_at"), now=emitted_at)
    # A generated link is never sent until the same process proves that its
    # validator accepts it. This catches configuration drift before delivery.
    validated = verify_followup_token(token)
    if validated.get("task_id") != str(task["task_id"]) or int(validated.get("v") or 0) != task_version:
        raise FollowupTokenError("followup_token_invalid")
    base = str(base_url or Config.CRM_BASE_URL).rstrip("/")
    if not base:
        raise FollowupConfigurationError("followup_base_url_missing")
    return f"{base}/followup/open/{quote(token, safe='')}"


def validate_followup_token_runtime() -> dict[str, Any]:
    """Run a local v2 issue/verify probe during application startup."""
    from config import Config

    config_status = Config.validate_followup_token_configuration()
    if not config_status.get("valid"):
        return config_status
    probe_now = datetime.now(timezone.utc)
    token = issue_followup_token("__startup_followup_probe__", now=probe_now)
    payload = verify_followup_token(token, now=probe_now)
    if payload.get("v") != TOKEN_VERSION or payload.get("task_id") != "__startup_followup_probe__":
        raise FollowupConfigurationError("followup_token_runtime_probe_failed")
    return {"valid": True, "version": TOKEN_VERSION, "probe": "ok", "production": bool(Config.IS_PRODUCTION)}


def _event_id(task_id: str, event_type: str, management_event_id: str | None = None) -> str:
    if event_type == "followup_management_created" and management_event_id:
        return f"followup:{task_id}:{event_type}:{management_event_id}"
    if event_type == "reminder_clicked":
        return f"followup:{task_id}:{event_type}"
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
    task = find_tracked_task(db, payload["task_id"], token_version=payload.get("v"))
    if not task:
        raise FollowupTokenError("followup_task_not_found")
    if task.get("status") in TERMINAL_UNATTRIBUTABLE_STATUSES or task.get("resolution") in {"superseded", "superseded_duplicate"}:
        raise FollowupTokenError("followup_task_unavailable")
    if str(entity_id) != task_entity_id(task):
        raise FollowupTokenError("followup_entity_mismatch")
    expected_actor = expected_actor_id(task, payload.get("v"))
    if not actor_user_id or not expected_actor or str(actor_user_id) != expected_actor:
        raise FollowupTokenError("followup_actor_forbidden")
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
    task = find_tracked_task(db, payload["task_id"], token_version=payload.get("v"))
    if not task:
        raise FollowupTokenError("followup_task_not_found")
    if task.get("status") in TERMINAL_UNATTRIBUTABLE_STATUSES or task.get("resolution") in {"superseded", "superseded_duplicate"}:
        raise FollowupTokenError("followup_task_unavailable")
    if str(entity_id) != task_entity_id(task):
        raise FollowupTokenError("followup_entity_mismatch")
    expected_actor = expected_actor_id(task, payload.get("v"))
    if not expected_actor or not executive_id or str(executive_id) != expected_actor:
        raise FollowupTokenError("followup_actor_forbidden")
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
