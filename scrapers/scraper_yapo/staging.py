"""Isolated, idempotent Yapo staging writes; never promotes or distributes records."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable

STAGING_DATABASE = "URLS"
STAGING_COLLECTION = "yapo_preimportacion"
OPERATIONAL_COLLECTION = "propiedades_captacion"
SOURCE_PORTAL = "yapo"
STAGING_INDEX_NAME = "source_portal_listing_id_unique"

_PROTECTED_FIELDS = {
    "_id", "manual_audit_observations", "manual_review_batches", "manual_review_responses",
    "manual_review", "classification_original", "classification_v3", "staging_only",
    "crm_promotion_allowed", "distribution_blocked", "assignment_ready", "assignment_runs",
    "distribution_runs", "notification_runs", "staging_batch_id", "staging_last_batch_id",
    "staging_created_at", "staging_last_imported_at", "canonical_source_sha256",
    "availability_status", "availability_checked_at", "availability_source", "availability_evidence",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_v3_payload(comparison: dict[str, Any], *, version: str, calculated_at: str) -> dict[str, Any]:
    """Build a compact V3 snapshot while preserving the canonical classifier field."""
    import json

    def parse(value: Any) -> Any:
        if not value:
            return []
        if isinstance(value, (list, dict)):
            return deepcopy(value)
        try:
            parsed = json.loads(str(value))
            return parsed
        except (TypeError, ValueError):
            return [str(value)]

    return {
        "state": str(comparison.get("proposed_state") or "INCIERTO"),
        "previous_state": str(comparison.get("prior_state") or ""),
        "reason": str(comparison.get("change_reason") or ""),
        "evidence": parse(comparison.get("classification_evidence")),
        "broker_evidence": parse(comparison.get("broker_evidence")),
        "owner_evidence": parse(comparison.get("owner_evidence")),
        "contradictions": parse(comparison.get("contradictions")),
        "owner_review_priority_signals": parse(comparison.get("owner_review_priority_signals")),
        "classifier_version": version,
        "deepseek_call_executed": False,
        "calculated_at": calculated_at,
    }


def _availability_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    status = str(record.get("availability_status") or "").upper()
    source = str(record.get("availability_source") or "").lower()
    checked_at = str(record.get("availability_checked_at") or "")
    evidence = record.get("availability_evidence") or []
    live_flashproxy = bool(
        status in {"ACTIVO", "DESACTIVADO"}
        and "flashproxy" in source
        and checked_at
    )
    if not live_flashproxy:
        return {
            "availability_status": "NO_VERIFICADO",
            "availability_checked_at": "",
            "availability_source": "not_live_verified",
            "availability_evidence": ["No existe comprobación actual explícita vía FlashProxy."],
        }
    return {
        "availability_status": status,
        "availability_checked_at": checked_at,
        "availability_source": source,
        "availability_evidence": deepcopy(evidence),
    }


def build_staging_update(
    record: dict[str, Any],
    *,
    v3_classification: dict[str, Any],
    batch_id: str,
    canonical_sha256: str = "",
    manual_observations: Iterable[dict[str, Any]] = (),
    manual_review_batch: dict[str, Any] | None = None,
    imported_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the identity query and update operators with human fields isolated.

    The canonical snapshot and original classification are insert-only. Re-runs
    may refresh V3/verified availability metadata but never replace observations,
    human review entries, comments, management, or the stored original label.
    """
    listing_id = str(record.get("listing_id") or "").strip()
    if not listing_id:
        raise ValueError("staging record is missing listing_id")
    now = imported_at or utc_now()
    identity = {"source_portal": SOURCE_PORTAL, "listing_id": listing_id}

    insert_doc = {
        key: deepcopy(value)
        for key, value in record.items()
        if key not in _PROTECTED_FIELDS
    }
    insert_doc.update({
        "source_portal": SOURCE_PORTAL,
        "origen": SOURCE_PORTAL,
        "source": "yapo_preimport_v3",
        "listing_id": listing_id,
        "classification_original": deepcopy(record.get("classification_original") or record.get("classification") or {}),
        "staging_batch_id": batch_id,
        "staging_created_at": now,
        "canonical_source_sha256": canonical_sha256 or str(record.get("canonical_source_sha256") or ""),
    })
    availability = _availability_snapshot(record)
    if availability["availability_source"] == "not_live_verified":
        insert_doc.update(availability)

    set_fields: dict[str, Any] = {
        "classification_v3": deepcopy(v3_classification),
        "staging_last_batch_id": batch_id,
        "staging_last_imported_at": now,
        "staging_only": True,
        "crm_promotion_allowed": False,
        "distribution_blocked": True,
        "assignment_ready": False,
    }
    # Current availability is refreshed only by an explicit FlashProxy result.
    if availability["availability_source"] != "not_live_verified":
        set_fields.update(availability)

    update: dict[str, Any] = {
        "$setOnInsert": insert_doc,
        "$set": set_fields,
    }
    observations = [deepcopy(item) for item in manual_observations if isinstance(item, dict)]
    if observations:
        update["$addToSet"] = {"manual_audit_observations": {"$each": observations}}
    return identity, update


def build_review_batch_entry(
    *, listing_id: str, batch_id: str, state: str, seed: int,
) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "sampling_seed": int(seed),
        "sampled_state": str(state),
        "status": "PENDING",
        "listing_id": str(listing_id),
        "comment": "",
        "verdict": None,
    }


def ensure_unique_identity_index(collection: Any, *, apply: bool) -> dict[str, Any]:
    """Verify no staging identity duplicates, then optionally create the unique index."""
    from collections import Counter

    duplicates: dict[str, int] = {}
    malformed = 0
    counts: Counter[tuple[str, str]] = Counter()
    for doc in collection.find({}, {"source_portal": 1, "listing_id": 1, "_id": 0}):
        source = str(doc.get("source_portal") or "")
        listing_id = str(doc.get("listing_id") or "")
        if source and listing_id:
            counts[(source, listing_id)] += 1
        else:
            malformed += 1
    duplicates = {f"{source}:{listing_id}": count for (source, listing_id), count in counts.items() if count > 1}
    if duplicates or malformed:
        raise RuntimeError(
            f"Staging identity integrity blocks unique index creation "
            f"({len(duplicates)} duplicate keys, {malformed} documents missing identity)."
        )

    desired = [("source_portal", 1), ("listing_id", 1)]
    indexes = collection.index_information()
    for name, info in indexes.items():
        if info.get("key") == desired and bool(info.get("unique")):
            return {"ready": True, "created": False, "name": name, "duplicates": 0}
    if not apply:
        return {"ready": False, "created": False, "name": STAGING_INDEX_NAME, "duplicates": 0}
    name = collection.create_index(desired, unique=True, name=STAGING_INDEX_NAME)
    return {"ready": True, "created": True, "name": name, "duplicates": 0}


def upsert_staging_record(
    collection: Any,
    record: dict[str, Any],
    *,
    v3_classification: dict[str, Any],
    batch_id: str,
    canonical_sha256: str = "",
    manual_observations: Iterable[dict[str, Any]] = (),
    manual_review_batch: dict[str, Any] | None = None,
    imported_at: str | None = None,
) -> dict[str, Any]:
    identity, update = build_staging_update(
        record,
        v3_classification=v3_classification,
        batch_id=batch_id,
        canonical_sha256=canonical_sha256,
        manual_observations=manual_observations,
        manual_review_batch=manual_review_batch,
        imported_at=imported_at,
    )
    result = collection.update_one(identity, update, upsert=True)
    review_added = False
    if manual_review_batch:
        review = deepcopy(manual_review_batch)
        review["listing_id"] = identity["listing_id"]
        guard = {**identity, "manual_review_batches.batch_id": {"$ne": review.get("batch_id")}}
        review_result = collection.update_one(guard, {"$push": {"manual_review_batches": review}})
        review_added = bool(getattr(review_result, "modified_count", 0))
    return {
        "listing_id": identity["listing_id"],
        "inserted": bool(getattr(result, "upserted_id", None) is not None),
        "matched": int(getattr(result, "matched_count", 0) or 0),
        "modified": int(getattr(result, "modified_count", 0) or 0),
        "review_batch_added": review_added,
    }
