"""Reconcile inbound leads that were waiting for the property inventory.

The reconciler is deliberately event-driven: it runs after a successful
inventory sync (or as a bounded repair of the existing pending-event backlog).
It does not poll all leads and it resumes the original commercial event using
the same provider message id, preserving idempotency and active assignments.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Iterable
from pymongo import ReturnDocument

from .commercial_intake import (
    COLLECTION,
    COMPLETED,
    RECONCILING,
    WAITING_INVENTORY,
    _now,
    process_inbound,
    property_reference_identity,
)
from .property_lookup import (
    canonical_portal,
    extract_property_external_id,
    normalize_property_url,
)

logger = logging.getLogger(__name__)

RECONCILE_LEASE_MINUTES = 10
RETRY_DELAY_SECONDS = 60
DEFAULT_RECONCILE_LIMIT = 200


def _property_active(prop: dict[str, Any]) -> bool:
    root = prop.get("disponible_prop360")
    if root is not None:
        return bool(root)
    state = prop.get("estado") or {}
    nested = state.get("disponible_prop360")
    if nested is not None:
        return bool(nested)
    status = str(state.get("estado_prop360") or prop.get("estado_prop360") or "").strip().lower()
    return not status or status not in {"baja", "pasiva", "no disponible", "inactiva", "inactive"}


def _property_identities(prop: dict[str, Any]) -> dict[str, set[str]]:
    """Extract normalized identities from a Prop360 document."""
    external_ids: set[str] = set()
    urls: set[str] = set()
    inventory_codes: set[str] = set()
    portals: set[str] = set()

    code = prop.get("codigo")
    if code not in (None, ""):
        inventory_codes.add(str(code).strip())

    publications = prop.get("publicaciones") or {}
    portal_data = publications.get("portal_inmobiliario") or {}
    candidates = [
        portal_data.get("url_mercado_libre"),
        portal_data.get("url_pi"),
        prop.get("source_url"),
        (prop.get("metadata") or {}).get("source_url"),
    ]
    candidates.extend(
        alias.get("url")
        for alias in publications.get("aliases", []) or []
        if isinstance(alias, dict)
    )

    for raw_url in candidates:
        if not raw_url:
            continue
        raw_url = str(raw_url).strip()
        portal = canonical_portal(raw_url)
        external_id = extract_property_external_id(raw_url, portal)
        normalized = normalize_property_url(raw_url)
        if portal:
            portals.add(portal)
        if external_id:
            external_ids.add(external_id.upper())
        if normalized:
            urls.add(normalized)

    return {
        "external_ids": external_ids,
        "urls": urls,
        "inventory_codes": inventory_codes,
        "portals": portals,
    }


def _merge_identities(properties: Iterable[dict[str, Any]] | None) -> dict[str, set[str]]:
    merged = {"external_ids": set(), "urls": set(), "inventory_codes": set(), "portals": set()}
    for prop in properties or []:
        if not isinstance(prop, dict) or not _property_active(prop):
            continue
        identity = _property_identities(prop)
        for key in merged:
            merged[key].update(identity[key])
    return merged


def _legacy_external_regexes(external_ids: Iterable[str]) -> list[dict]:
    clauses = []
    for external_id in external_ids:
        match = re.match(r"^([A-Za-z]+)[-_]?(\d+)$", str(external_id))
        if match:
            pattern = f"{re.escape(match.group(1))}[-_]?{re.escape(match.group(2))}"
        else:
            pattern = re.escape(str(external_id))
        clauses.append({"source_property_code": {"$regex": pattern, "$options": "i"}})
    return clauses


def _candidate_events(db, identities: dict[str, set[str]], limit: int) -> list[dict]:
    coll = db[COLLECTION]
    base = {"commercial_processing_state": WAITING_INVENTORY}
    clauses = []
    if identities["external_ids"]:
        clauses.append({"source_external_id": {"$in": list(identities["external_ids"])}})
        clauses.extend(_legacy_external_regexes(identities["external_ids"]))
    if identities["urls"]:
        clauses.append({"source_url_normalized": {"$in": list(identities["urls"])}})
    if identities["inventory_codes"]:
        clauses.extend([
            {"source_inventory_code": {"$in": list(identities["inventory_codes"])}},
            {"source_property_code": {"$in": list(identities["inventory_codes"])}},
        ])

    if clauses:
        base["$or"] = clauses
    return list(coll.find(base).sort("created_at", 1).limit(max(1, int(limit))))


def _backlog_events(db, limit: int) -> list[dict]:
    """Return a bounded pending backlog for recovery after partial syncs."""
    now = _now()
    query = {
        "commercial_processing_state": WAITING_INVENTORY,
        "$or": [
            {"next_attempt_at": {"$exists": False}},
            {"next_attempt_at": {"$lte": now}},
        ],
    }
    return list(db[COLLECTION].find(query).sort("created_at", 1).limit(max(1, int(limit))))


def _event_matches_inventory(event: dict, identities: dict[str, set[str]]) -> bool:
    raw = event.get("source_property_code")
    derived = property_reference_identity(raw)
    external_id = str(derived.get("source_external_id") or event.get("source_external_id") or "").upper()
    normalized_url = derived.get("source_url_normalized") or event.get("source_url_normalized")
    inventory_code = str(derived.get("source_inventory_code") or event.get("source_inventory_code") or "").strip()
    return bool(
        (external_id and external_id in identities["external_ids"])
        or (normalized_url and normalized_url in identities["urls"])
        or (inventory_code and inventory_code in identities["inventory_codes"])
    )


def _claim_event(db, event_id):
    now = _now()
    lease_until = now + timedelta(minutes=RECONCILE_LEASE_MINUTES)
    return db[COLLECTION].find_one_and_update(
        {
            "_id": event_id,
            "commercial_processing_state": WAITING_INVENTORY,
            "$or": [
                {"reconcile_lease_until": {"$exists": False}},
                {"reconcile_lease_until": {"$lte": now}},
            ],
        },
        {
            "$set": {
                "commercial_processing_state": RECONCILING,
                "reconcile_started_at": now,
                "reconcile_lease_until": lease_until,
                "updated_at": now,
            },
            "$inc": {"reconcile_attempts": 1},
            "$push": {"history": {"at": now, "state": RECONCILING, "reason": "inventory_sync"}},
        },
        return_document=ReturnDocument.AFTER,
    )


def _source_message(db, event: dict) -> tuple[str | None, Any]:
    provider_id = event.get("source_inbound_provider_id")
    if provider_id:
        job = db["chatbot_inbound_jobs"].find_one(
            {"inbound_provider_message_id": str(provider_id)},
            {"text": 1, "received_at": 1},
        )
        if job and job.get("text"):
            return str(job["text"]), job.get("received_at") or event.get("received_at")

    if event.get("source_message"):
        return str(event["source_message"]), event.get("received_at")

    lead = db["leads"].find_one({"_id": event.get("lead_id")}, {"messages": {"$slice": -20}})
    for message in reversed((lead or {}).get("messages") or []):
        if message.get("role") == "user" and message.get("content"):
            return str(message["content"]), event.get("received_at")
    return None, event.get("received_at")


def _release_event(db, event_id, reason: str) -> None:
    now = _now()
    db[COLLECTION].update_one(
        {"_id": event_id, "commercial_processing_state": RECONCILING},
        {
            "$set": {
                "commercial_processing_state": WAITING_INVENTORY,
                "next_attempt_at": now + timedelta(seconds=RETRY_DELAY_SECONDS),
                "last_reconcile_error": reason,
                "updated_at": now,
            },
            "$unset": {"reconcile_lease_until": ""},
            "$push": {"history": {"at": now, "state": WAITING_INVENTORY, "reason": reason}},
        },
    )


def resume_waiting_event(db, event: dict) -> dict:
    """Resume one existing waiting event without creating a new inbound event."""
    claimed = _claim_event(db, event.get("_id"))
    if not claimed:
        return {"status": "skipped", "reason": "already_claimed_or_not_waiting"}

    text, received_at = _source_message(db, claimed)
    if not text:
        _release_event(db, claimed["_id"], "original_message_unavailable")
        return {"status": "failed", "reason": "original_message_unavailable", "event_id": str(claimed["_id"])}

    try:
        result = process_inbound(
            db,
            inbound_provider_id=claimed.get("source_inbound_provider_id"),
            phone=claimed.get("phone"),
            text=text,
            received_at=received_at,
            is_test=bool(claimed.get("is_test")),
        ) or db[COLLECTION].find_one({"_id": claimed["_id"]}) or {}
        if result.get("commercial_processing_state") == COMPLETED:
            db[COLLECTION].update_one(
                {"_id": claimed["_id"]},
                {"$unset": {"reconcile_lease_until": "", "last_reconcile_error": ""}},
            )
            return {"status": "completed", "event_id": str(claimed["_id"]), "lead_id": str(result.get("lead_id") or claimed.get("lead_id"))}
        reason = str(result.get("commercial_processing_state") or "property_still_unresolved")
        _release_event(db, claimed["_id"], reason)
        return {"status": "waiting", "reason": reason, "event_id": str(claimed["_id"])}
    except Exception as exc:
        logger.exception("[INVENTORY_RECONCILE] event=%s failed", claimed.get("_id"))
        _release_event(db, claimed["_id"], f"{type(exc).__name__}:{exc}")
        return {"status": "failed", "reason": str(exc), "event_id": str(claimed["_id"])}


def reconcile_waiting_inventory_events(db, properties: Iterable[dict[str, Any]] | None = None,
                                       limit: int = DEFAULT_RECONCILE_LIMIT) -> dict:
    """Resolve only pending inventory events after a successful sync."""
    identities = _merge_identities(properties)
    events = _candidate_events(db, identities, limit) if any(identities.values()) else _backlog_events(db, limit)
    if identities:
        events = [event for event in events if _event_matches_inventory(event, identities)]

    report = {"candidates": len(events), "completed": 0, "waiting": 0, "failed": 0, "skipped": 0}
    for event in events:
        result = resume_waiting_event(db, event)
        status = result.get("status")
        if status in report:
            report[status] += 1
        else:
            report["skipped"] += 1
    logger.info("[INVENTORY_RECONCILE] %s", report)
    return report


__all__ = ["reconcile_waiting_inventory_events", "resume_waiting_event"]
