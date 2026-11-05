"""Safely normalize the two approved Yapo lots in URLS.propiedades_captacion.

Default mode is a read-only Atlas dry-run plus a complete local pre-write
backup. ``--apply`` requires that backup and its SHA-256, rechecks the exact
source snapshot in an Atlas transaction, writes only allowlisted dotted paths
with ``$set``, and verifies the transaction and post-commit readback.

This module does not contact Yapo, call an LLM, create assignments, or trigger
distribution/notifications.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bson import json_util
from bson.json_util import CANONICAL_JSON_OPTIONS
from pymongo import MongoClient, UpdateOne

from canonical_classification import CANONICAL_CLASSIFICATION_VERSION
from captacion_materialized import build_captacion_materialized_fields
from scrapers.scraper_yapo.downloader import validate_html
from scrapers.scraper_yapo.extractor import extract_yapo_listing_images
from scrapers.scraper_yapo.mongo_store import MongoStore

TARGET_DATABASE = "URLS"
TARGET_COLLECTION = "propiedades_captacion"
CANONICAL_SOURCE_SHA256 = "dc9bda51a093abd9a1d7c6a741eeacac275724ea74b29ad4cc2a8bba558e12bf"
CANONICAL_BATCH_ID = "yapo_maipu_482_20261103_v3"
LIVE_BATCH_ID = "issue122_maipu_20261104_141023_35cf"
EXPECTED_CANONICAL_COUNT = 482
EXPECTED_LIVE_COUNT = 37
EXPECTED_INCOMPLETE_IDS = {"33213261", "33215722", "33215751"}

LEGACY_TO_CANONICAL = {
    "CORREDOR_SEGURO": "BROKER_CONFIRMED",
    "CORREDOR_PROBABLE": "BROKER_PROBABLE",
    "DUEÑO_SEGURO": "OWNER_CONFIRMED",
    "DUEÑO_PROBABLE": "OWNER_PROBABLE",
    "INCIERTO": "UNCERTAIN",
}
CANONICAL_TO_LEGACY = {value: key for key, value in LEGACY_TO_CANONICAL.items()}
IDENTITY_CANONICAL_STATES = frozenset(CANONICAL_TO_LEGACY)

MATERIALIZED_FIELDS = tuple(build_captacion_materialized_fields({}).keys())
PHOTO_FIELDS = (
    "image_urls", "images", "enlaces_fotos", "image_urls_count",
    "image_urls_detected_count", "main_image_url",
    "details.image_urls", "details.images", "details.enlaces_fotos",
    "details.image_urls_count", "details.image_urls_detected_count",
    "details.main_image_url",
)
CLASSIFICATION_SYNC_FIELDS = (
    "previous_state", "reason", "classification_reason", "evidence",
    "evidence_strength", "classification_rule_ids", "owner_evidence",
    "broker_evidence", "contradictions", "missing_information",
    "owner_review_priority_signals", "owner_signal_candidates",
    "owner_signal_evidence", "owner_classification_confirmed",
    "family_or_representative_signals", "direct_deal_with_commercial_contradictions",
    "company_like_suspected", "company_like_evidence", "contextual_analysis",
    "profile_identity_signal", "confidence", "confidence_type", "requires_review",
    "review_reason", "classifier_version", "calculated_at",
)
CLASSIFICATION_LIST_FIELDS = frozenset({
    "evidence", "classification_rule_ids", "owner_evidence", "broker_evidence",
    "contradictions", "missing_information", "owner_review_priority_signals",
    "owner_signal_candidates", "owner_signal_evidence", "family_or_representative_signals",
    "direct_deal_with_commercial_contradictions", "company_like_evidence",
})
ASSIGNMENT_OR_CRM_PATHS = frozenset({
    "assignment_ready", "exclude_from_assignment", "gestion.ejecutivo_id",
    "gestion.ejecutivo_asignado", "assigned_to", "assigned_executive", "asignacion",
    "asignaciones", "executive_assignments", "captacion_assignment",
    "captacion_assigned_to", "manual_review_observations", "manual_audit_observations",
    "manual_review_batches", "gestion", "notas", "actividades", "captacion_management_events",
})


def _get_path(document: dict[str, Any], path: str) -> Any:
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _has_path(document: dict[str, Any], path: str) -> bool:
    value: Any = document
    parts = path.split(".")
    for part in parts[:-1]:
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return isinstance(value, dict) and parts[-1] in value


def _set_path(document: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    parent = document
    for part in parts[:-1]:
        child = parent.get(part)
        if not isinstance(child, dict):
            child = {}
            parent[part] = child
        parent = child
    parent[parts[-1]] = deepcopy(value)


def _state(value: Any) -> str:
    return str(value or "").strip().upper().replace("DUENO", "DUEÑO")


def _ordered_union(left: Any, right: Any) -> list[Any]:
    values: list[Any] = []
    seen: set[str] = set()
    for source in (left, right):
        rows = source if isinstance(source, (list, tuple)) else ([] if source is None else [source])
        for item in rows:
            marker = json_util.dumps(item, json_options=CANONICAL_JSON_OPTIONS, sort_keys=True)
            if marker not in seen:
                seen.add(marker)
                values.append(deepcopy(item))
    return values


def _canonical_final_for_document(document: dict[str, Any]) -> tuple[str, str]:
    """Choose V3 only when the saved canonical final does not already decide.

    A non-UNCERTAIN canonical final is preserved. This retains professional
    hard-veto decisions (including four historical rows whose legacy state was
    later drifted) while allowing V3 to resolve the 31 uncertain records with
    new owner/broker evidence. Human-confirmed decisions are rejected by the
    batch preflight before this helper is used for writes.
    """
    classification = document.get("classification") if isinstance(document.get("classification"), dict) else {}
    current_final = _state(classification.get("final"))
    v3_state = _state((document.get("classification_v3") or {}).get("state"))
    current_state = _state(classification.get("state"))

    if current_final in IDENTITY_CANONICAL_STATES and current_final != "UNCERTAIN":
        return current_final, "preserve_existing_canonical_final"
    if v3_state in LEGACY_TO_CANONICAL:
        return LEGACY_TO_CANONICAL[v3_state], "classification_v3_state"
    if current_final in IDENTITY_CANONICAL_STATES:
        return current_final, "preserve_existing_uncertain_final"
    if current_state in LEGACY_TO_CANONICAL:
        return LEGACY_TO_CANONICAL[current_state], "existing_classification_state"
    raise ValueError(f"Unsupported classification for Yapo listing {document.get('listing_id')}.")


def _is_professional_final_override(document: dict[str, Any], desired_final: str, old_state: str) -> bool:
    classification = document.get("classification") if isinstance(document.get("classification"), dict) else {}
    return bool(
        desired_final == "BROKER_CONFIRMED"
        and old_state != "CORREDOR_SEGURO"
        and (
            _state(classification.get("hard_veto")) == "PROFESSIONAL"
            or classification.get("hard_broker_veto") is True
            or classification.get("professional_hard_veto") is True
        )
    )


def _set_if_different(document: dict[str, Any], updates: dict[str, Any], path: str, value: Any) -> None:
    if not _has_path(document, path) or not _values_equal(_get_path(document, path), value):
        updates[path] = deepcopy(value)


def _classification_updates(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    main = document.get("classification") if isinstance(document.get("classification"), dict) else {}
    v3 = document.get("classification_v3") if isinstance(document.get("classification_v3"), dict) else {}
    listing_id = str(document.get("listing_id") or "")
    human_confirmed = MongoStore._human_confirmed_classification(main)
    if human_confirmed:
        raise RuntimeError(f"Human-confirmed classification blocks automation for {listing_id}.")

    desired_final, decision_source = _canonical_final_for_document(document)
    desired_state = CANONICAL_TO_LEGACY[desired_final]
    old_state = _state(main.get("state"))
    updates: dict[str, Any] = {}

    for path, value in (
        ("classification.state", desired_state),
        ("classification.classification_state", desired_state),
        ("classification.final_state", desired_state),
        ("classification.final", desired_final),
        ("classification.identity_classification", desired_final),
        ("classification.canonical_classification_version", CANONICAL_CLASSIFICATION_VERSION),
        ("classification.classification_serialization_version", "canonical-persistence-v1"),
    ):
        _set_if_different(document, updates, path, value)

    state_changed = old_state != desired_state
    state_matches_v3 = old_state == _state(v3.get("state"))
    use_v3_detail = decision_source == "classification_v3_state" and (state_changed or not state_matches_v3)
    professional_override = _is_professional_final_override(document, desired_final, old_state)
    sync_v3_detail = use_v3_detail or professional_override

    if sync_v3_detail:
        for key in CLASSIFICATION_SYNC_FIELDS:
            if key not in v3:
                continue
            new_value = v3[key]
            if key in CLASSIFICATION_LIST_FIELDS:
                new_value = _ordered_union(main.get(key), new_value)
            _set_if_different(document, updates, f"classification.{key}", new_value)

    current_reason = str(main.get("reason") or main.get("classification_reason") or "").strip()
    current_evidence = _ordered_union(main.get("evidence"), v3.get("evidence") if sync_v3_detail else None)
    if professional_override:
        reason = (
            "CORREDOR_SEGURO preservado por el final canónico existente y el veto profesional "
            "(hard_veto=PROFESSIONAL); classification_v3 conserva su estado base para auditoría."
        )
        current_evidence = _ordered_union(
            current_evidence,
            ["professional_hard_veto:PROFESSIONAL", "canonical_final_preserved:BROKER_CONFIRMED"],
        )
        decision_source = "preserve_professional_hard_veto"
    elif use_v3_detail:
        reason = str(v3.get("reason") or v3.get("classification_reason") or current_reason).strip()
    else:
        reason = current_reason or f"Estado canónico normalizado desde {decision_source}."

    refresh_decision_evidence = bool(
        use_v3_detail
        or professional_override
        or not _has_path(document, "classification.final_evidence")
        or not _has_path(document, "classification.final_reason")
    )
    if reason and (use_v3_detail or professional_override or not _has_path(document, "classification.reason")):
        for path in ("classification.reason", "classification.classification_reason"):
            _set_if_different(document, updates, path, reason)
    if reason and refresh_decision_evidence:
        _set_if_different(document, updates, "classification.final_reason", reason)
    if refresh_decision_evidence:
        for path in ("classification.final_evidence", "classification.evidence", "classification_evidence"):
            _set_if_different(document, updates, path, current_evidence)

    if not _has_path(document, "classification.state_source") or state_changed:
        source_value = (
            "classification.professional_hard_veto" if professional_override
            else "classification_v3.reconciled" if use_v3_detail
            else str(main.get("state_source") or "classification_v3.state")
        )
        _set_if_different(document, updates, "classification.state_source", source_value)

    return updates, {
        "old_state": old_state,
        "v3_state": _state(v3.get("state")),
        "old_final": _state(main.get("final")),
        "new_state": desired_state,
        "new_final": desired_final,
        "decision_source": decision_source,
        "human_confirmed": human_confirmed,
        "professional_override": professional_override,
        "v3_detail_copied": use_v3_detail,
    }


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, datetime) and isinstance(right, datetime):
        left = left.replace(tzinfo=timezone.utc) if left.tzinfo is None else left.astimezone(timezone.utc)
        right = right.replace(tzinfo=timezone.utc) if right.tzinfo is None else right.astimezone(timezone.utc)
        # BSON Date stores milliseconds; MongoDB readback truncates the
        # microseconds that Python datetimes may carry from ISO timestamps.
        left = left.replace(microsecond=(left.microsecond // 1000) * 1000)
        right = right.replace(microsecond=(right.microsecond // 1000) * 1000)
        return left == right
    if isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(right, (int, float)) and not isinstance(right, bool):
        return math.isclose(float(left), float(right), rel_tol=0, abs_tol=1e-9)
    return left == right


def _photo_updates(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    listing_id = str(document.get("listing_id") or "")
    path_text = str(document.get("html_source_path") or document.get("html_path") or "").strip()
    html_path = Path(path_text) if path_text else None
    validation_status = str(document.get("html_validation_status") or "").upper()
    images: list[str] = []
    integrity: dict[str, Any] = {"html_path": path_text, "html_validation_status": validation_status}

    if html_path is not None and html_path.is_file():
        raw = html_path.read_bytes()
        expected_sha = str(document.get("html_sha256") or "").strip().lower()
        actual_sha = hashlib.sha256(raw).hexdigest()
        if expected_sha and actual_sha != expected_sha:
            raise RuntimeError(f"Local HTML SHA-256 does not match Mongo for {listing_id}.")
        html = raw.decode("utf-8", errors="replace")
        validation = validate_html(html)
        if validation.get("status") != "OK" or validation_status != "OK":
            raise RuntimeError(f"Local HTML is not a valid saved listing for {listing_id}: {validation}.")
        images = extract_yapo_listing_images(html, listing_id, str(document.get("url") or ""))
        if not images:
            raise RuntimeError(f"No listing-ID-bound photo assets found in valid local HTML for {listing_id}.")
        integrity.update({"html_sha256": actual_sha, "html_status_revalidated": validation.get("status")})
    else:
        if listing_id not in EXPECTED_INCOMPLETE_IDS:
            raise RuntimeError(f"Missing local HTML is not an approved blocked exception: {listing_id}.")
        if listing_id == "33213261":
            if validation_status != "NO_VERIFICADO" or str(document.get("scrape_stage") or "") != "needs_rescrape":
                raise RuntimeError("33213261 must remain technically blocked as NO_VERIFICADO/needs_rescrape.")
            if str(document.get("availability_status") or "") == "DESACTIVADO":
                raise RuntimeError("HTTP 403 must not be converted to DESACTIVADO.")
            integrity["blocked_reason"] = "HTML local ausente tras HTTP 403; sin nueva solicitud a Yapo."
        else:
            if validation_status != "LISTING_REMOVED" or str(document.get("scrape_stage") or "") != "ad_removed":
                raise RuntimeError(f"{listing_id} must retain its locally recorded removed-ad status.")
            integrity["blocked_reason"] = "Anuncio marcado como eliminado en evidencia guardada; no se inventan fotos."

    details = document.get("details")
    if details is not None and not isinstance(details, dict):
        raise RuntimeError(f"details is not an object for Yapo listing {listing_id}.")
    if images:
        primary = images[0]
    else:
        primary = ""
    values = {
        "image_urls": images,
        "images": images,
        "enlaces_fotos": images,
        "image_urls_count": len(images),
        "image_urls_detected_count": len(images),
        "main_image_url": primary,
        "details.image_urls": images,
        "details.images": images,
        "details.enlaces_fotos": images,
        "details.image_urls_count": len(images),
        "details.image_urls_detected_count": len(images),
        "details.main_image_url": primary,
    }
    updates: dict[str, Any] = {}
    for path, value in values.items():
        _set_if_different(document, updates, path, value)
    integrity["image_count"] = len(images)
    integrity["image_urls"] = images
    return updates, integrity


def _materialized_updates(document: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    desired = build_captacion_materialized_fields(document)
    updates: dict[str, Any] = {}
    preserved = []
    for field, value in desired.items():
        present = _has_path(document, field)
        old = _get_path(document, field)
        if value is None:
            # Missing inputs stay missing; null is not a fabricated date/price/phone.
            continue
        if present and old is not None:
            if not _values_equal(old, value):
                raise RuntimeError(
                    f"Existing materialized field {field} conflicts with shared materializer "
                    f"for listing {document.get('listing_id')}."
                )
            preserved.append(field)
            continue
        if not present or old is None:
            updates[field] = deepcopy(value)
    return updates, preserved


def _batch_marker(document: dict[str, Any]) -> str:
    ingestion = document.get("yapo_ingestion") if isinstance(document.get("yapo_ingestion"), dict) else {}
    if ingestion.get("canonical_source_sha256") == CANONICAL_SOURCE_SHA256:
        return "canonical_482"
    if ingestion.get("source_batch_id") == LIVE_BATCH_ID:
        return "live_37"
    return "unrecognized"


def _cohort_query() -> dict[str, Any]:
    return {
        "origen": "yapo",
        "$or": [
            {"yapo_ingestion.canonical_source_sha256": CANONICAL_SOURCE_SHA256},
            {"yapo_ingestion.source_batch_id": LIVE_BATCH_ID},
        ],
    }


def _history_query() -> dict[str, Any]:
    return {
        "origen": "yapo",
        "$nor": [
            {"yapo_ingestion.canonical_source_sha256": CANONICAL_SOURCE_SHA256},
            {"yapo_ingestion.source_batch_id": LIVE_BATCH_ID},
        ],
    }


def _assert_cohort_invariants(documents: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [str(document.get("listing_id") or "").strip() for document in documents]
    counts = Counter(_batch_marker(document) for document in documents)
    if len(documents) != EXPECTED_CANONICAL_COUNT + EXPECTED_LIVE_COUNT:
        raise RuntimeError(f"Expected exactly 519 Yapo records; got {len(documents)}.")
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        raise RuntimeError("Cohort has missing or duplicate canonical listing IDs.")
    if counts != Counter({"canonical_482": EXPECTED_CANONICAL_COUNT, "live_37": EXPECTED_LIVE_COUNT}):
        raise RuntimeError(f"Source cohort markers changed: {dict(counts)}.")
    if any(str(document.get("origen") or "").casefold() != "yapo" for document in documents):
        raise RuntimeError("Cohort contains a document from another portal.")

    human_confirmed = [
        str(d.get("listing_id")) for d in documents
        if MongoStore._human_confirmed_classification(d.get("classification"))
    ]
    if human_confirmed:
        raise RuntimeError(f"Human-confirmed classifications must not be overwritten: {human_confirmed[:10]}.")

    assignment_ready = [
        str(d.get("listing_id")) for d in documents
        if (d.get("classification") or {}).get("assignment_ready") is True or d.get("assignment_ready") is True
    ]
    excluded = [
        str(d.get("listing_id")) for d in documents
        if (d.get("classification") or {}).get("exclude_from_assignment") is not True
    ]
    assigned = [
        str(d.get("listing_id")) for d in documents
        if any(_get_path(d, path) not in (None, "", [], {}) for path in (
            "gestion.ejecutivo_id", "gestion.ejecutivo_asignado", "assigned_to",
            "assigned_executive", "asignacion", "asignaciones", "executive_assignments",
        ))
    ]
    if assignment_ready or excluded or assigned:
        raise RuntimeError(
            "Assignment guard failed: "
            f"ready={assignment_ready[:8]}, not_excluded={excluded[:8]}, assigned={assigned[:8]}."
        )
    live = [d for d in documents if _batch_marker(d) == "live_37"]
    verified_active = [d for d in live if str(d.get("availability_status") or "").upper() == "ACTIVO"]
    if verified_active:
        raise RuntimeError("The live batch unexpectedly contains ACTIVE records; normalization is blocked.")
    return {
        "total": len(documents),
        "canonical_482": counts["canonical_482"],
        "live_37": counts["live_37"],
        "unique_listing_ids": len(set(ids)),
        "human_confirmed": len(human_confirmed),
        "assignment_ready": len(assignment_ready),
        "excluded_from_assignment": len(documents) - len(excluded),
        "assigned": len(assigned),
        "live_active": len(verified_active),
    }


def _documents_signature(documents: list[dict[str, Any]]) -> str:
    ordered = sorted(documents, key=lambda d: str(d.get("listing_id") or ""))
    serialized = json_util.dumps(
        ordered,
        json_options=CANONICAL_JSON_OPTIONS,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _mongo_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, datetime):
        return "date"
    return type(value).__name__


def _collection_type_summary(collection: Any, match: dict[str, Any], paths: list[str]) -> dict[str, dict[str, int]]:
    projection = {f"field_{index}": {"$type": f"${path}"} for index, path in enumerate(paths)}
    items = [
        {"field": path, "type": f"$field_{index}"}
        for index, path in enumerate(paths)
    ]
    pipeline = [
        {"$match": match},
        {"$project": projection},
        {"$project": {"items": items}},
        {"$unwind": "$items"},
        {"$group": {"_id": {"field": "$items.field", "type": "$items.type"}, "count": {"$sum": 1}}},
    ]
    summary: dict[str, dict[str, int]] = {path: {} for path in paths}
    for row in collection.aggregate(pipeline, allowDiskUse=False):
        key = row.get("_id") or {}
        field = str(key.get("field"))
        if field in summary:
            summary[field][str(key.get("type"))] = int(row.get("count") or 0)
    return summary


def _type_compatible_with_history(candidate_type: str, observed: dict[str, int]) -> bool:
    actual = {key for key, count in observed.items() if count and key != "missing"}
    if not actual:
        return True
    numeric = {"int", "long", "double", "decimal"}
    if candidate_type in numeric and actual <= numeric:
        return True
    return candidate_type in actual


def _schema_report(collection: Any, documents: list[dict[str, Any]], plan: list[dict[str, Any]]) -> dict[str, Any]:
    planned_paths = sorted({path for row in plan for path in row["updates"]})
    schema_paths = sorted(set(planned_paths) | set(MATERIALIZED_FIELDS) | set(PHOTO_FIELDS) | {
        "classification.state", "classification.classification_state", "classification.final_state",
        "classification.final", "classification.identity_classification", "classification.confidence",
        "classification.owner_probability", "classification.assignment_ready",
        "classification.exclude_from_assignment", "classification.final_reason",
        "classification.final_evidence", "classification_evidence",
    })
    historic_query = _history_query()
    collection_all_yapo = {"origen": "yapo"}
    historic_types = _collection_type_summary(collection, historic_query, schema_paths)
    all_yapo_types = _collection_type_summary(collection, collection_all_yapo, schema_paths)
    before_types = {path: dict(Counter(_mongo_type(_get_path(doc, path)) for doc in documents if _has_path(doc, path))) for path in schema_paths}
    after_counter: dict[str, Counter[str]] = {path: Counter() for path in schema_paths}
    incompatibilities: list[dict[str, str]] = []
    for document in documents:
        row = next((item for item in plan if item["listing_id"] == str(document.get("listing_id"))), None)
        updates = row["updates"] if row else {}
        for path in schema_paths:
            value = updates.get(path, _get_path(document, path))
            present = path in updates or _has_path(document, path)
            if present:
                value_type = _mongo_type(value)
                after_counter[path][value_type] += 1
                if path in updates and value is not None and not _type_compatible_with_history(value_type, historic_types.get(path, {})):
                    incompatibilities.append({
                        "listing_id": str(document.get("listing_id")),
                        "field": path,
                        "new_type": value_type,
                        "historic_types": ",".join(sorted(k for k, n in historic_types.get(path, {}).items() if n)),
                    })
    if incompatibilities:
        raise RuntimeError(f"Planned BSON types conflict with Yapo history: {incompatibilities[:12]}.")
    return {
        "historical_yapo_documents": collection.count_documents(historic_query),
        "all_yapo_documents": collection.count_documents(collection_all_yapo),
        "fields": {
            path: {
                "historical_yapo_type_counts": historic_types.get(path, {}),
                "all_yapo_type_counts": all_yapo_types.get(path, {}),
                "target_before_type_counts": before_types.get(path, {}),
                "target_after_type_counts": dict(after_counter[path]),
            }
            for path in schema_paths
        },
        "planned_type_incompatibilities": 0,
        "confidence_contract": "classification.confidence se conserva null/ausente: sin una fuente numérica demostrada no se fabrica.",
        "owner_probability_contract": "No se escribe classification.owner_probability; captacion_probability_sort usa el -1 del materializador cuando no hay probabilidad.",
        "consumer_contract": {
            "captacion_materialized_fields": "captacion_materialized.build_captacion_materialized_fields",
            "api_captacion_consumers": [
                "captacion_priority", "captacion_sort_date", "captacion_management_date",
                "captacion_comuna_sort", "captacion_price_sort", "captacion_probability_sort",
                "telefono_normalizado",
            ],
            "photo_consumers": ["image_urls", "images", "enlaces_fotos", "main_image_url", "details.*"],
        },
    }


def _plan(collection: Any, documents: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cohort = _assert_cohort_invariants(documents)
    rows: list[dict[str, Any]] = []
    total_images = 0
    live_image_counts: Counter[str] = Counter()
    classification_before = Counter()
    classification_after = Counter()
    v3_disagreements = 0
    v3_reconciled = 0
    professional_overrides = 0
    added_fields = Counter()
    for document in sorted(documents, key=lambda d: str(d.get("listing_id") or "")):
        listing_id = str(document["listing_id"])
        marker = _batch_marker(document)
        old_class = document.get("classification") if isinstance(document.get("classification"), dict) else {}
        classification_before[_state(old_class.get("state"))] += 1
        if _state(old_class.get("state")) != _state((document.get("classification_v3") or {}).get("state")):
            v3_disagreements += 1

        updates: dict[str, Any] = {}
        class_updates, decision = _classification_updates(document)
        updates.update(class_updates)
        classification_after[decision["new_state"]] += 1
        if decision["old_state"] != decision["new_state"] and decision["v3_state"] == decision["new_state"]:
            v3_reconciled += 1
        if decision["professional_override"]:
            professional_overrides += 1

        if marker == "live_37":
            photo_updates, photo_report = _photo_updates(document)
            updates.update(photo_updates)
            total_images += int(photo_report["image_count"])
            live_image_counts[listing_id] = int(photo_report["image_count"])
        else:
            photo_report = {"image_count": len(document.get("image_urls") or []), "preserved_canonical_photos": True}

        material_updates, preserved_materials = _materialized_updates(document)
        updates.update(material_updates)
        for path in updates:
            if not _has_path(document, path):
                added_fields[path] += 1

        if any(path in ASSIGNMENT_OR_CRM_PATHS or path.startswith(("gestion.", "asignacion.", "crm_", "executive_")) for path in updates):
            raise RuntimeError(f"Plan attempted to modify CRM/assignment data for {listing_id}.")
        if any(path.endswith("owner_probability") for path in updates):
            raise RuntimeError(f"Plan attempted to fabricate owner_probability for {listing_id}.")

        deltas = []
        for path, new_value in sorted(updates.items()):
            present = _has_path(document, path)
            deltas.append({
                "field": path,
                "old_present": present,
                "old": deepcopy(_get_path(document, path)) if present else None,
                "new": deepcopy(new_value),
            })
        rows.append({
            "listing_id": listing_id,
            "source_lot": marker,
            "classification_decision": decision,
            "photo_audit": photo_report,
            "materialized_existing_values_preserved": preserved_materials,
            "updates": updates,
            "deltas": deltas,
        })

    if total_images != 733 or sum(live_image_counts.values()) != 733:
        raise RuntimeError(f"Expected 733 ID-bound photos in 34 valid local pages; found {total_images}.")
    if set(live_image_counts) != set(EXPECTED_INCOMPLETE_IDS) | {r["listing_id"] for r in rows if r["source_lot"] == "live_37" and r["photo_audit"]["image_count"] > 0}:
        raise RuntimeError("Photo accounting did not cover the exact live batch.")

    schema = _schema_report(collection, documents, rows)
    summary = {
        "cohort": cohort,
        "classification_before": dict(classification_before),
        "classification_after": dict(classification_after),
        "v3_disagreements": v3_disagreements,
        "v3_records_reconciled_without_hard_veto": v3_reconciled,
        "professional_final_overrides_preserved": professional_overrides,
        "live_photo_urls_before": sum(len(d.get("image_urls") or []) for d in documents if _batch_marker(d) == "live_37"),
        "live_photo_urls_after": total_images,
        "live_photo_counts_by_id": dict(live_image_counts),
        "canonical_482_photo_urls_preserved": sum(len(d.get("image_urls") or []) for d in documents if _batch_marker(d) == "canonical_482"),
        "fields_added_by_path": dict(added_fields),
        "documents_with_updates": sum(bool(row["updates"]) for row in rows),
        "total_dotted_field_updates": sum(len(row["updates"]) for row in rows),
        "blocked_incomplete_records": sorted(EXPECTED_INCOMPLETE_IDS),
        "schema": schema,
    }
    return rows, summary


def _backup_documents(documents: list[dict[str, Any]], backup_path: Path) -> str:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json_util.dumps(
        {"documents": documents},
        json_options=CANONICAL_JSON_OPTIONS,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    tmp_path = backup_path.with_suffix(backup_path.suffix + ".tmp")
    tmp_path.write_bytes(payload)
    os.replace(tmp_path, backup_path)
    digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    return digest


def _load_backup(path: Path, expected_sha256: str) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError("Backup SHA-256 does not match the supplied approval hash.")
    payload = json_util.loads(raw.decode("utf-8"))
    documents = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(documents, list) or len(documents) != 519:
        raise RuntimeError("Backup must contain the exact 519-document pre-write snapshot.")
    return documents


def _report_path(config: Any, mode: str) -> Path:
    report_dir = config.reports_dir / "issue122_yapo_normalization"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return report_dir / f"yapo_519_{mode}_{stamp}.json"


def _write_report(path: Path, report: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json_util.dumps(report, json_options=CANONICAL_JSON_OPTIONS, ensure_ascii=False, indent=2).encode("utf-8")
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(payload)
    os.replace(tmp_path, path)
    return hashlib.sha256(payload).hexdigest()


def _document_identity_filter(document: dict[str, Any]) -> dict[str, Any]:
    marker = _batch_marker(document)
    query: dict[str, Any] = {"_id": document["_id"], "origen": "yapo", "listing_id": document["listing_id"]}
    if marker == "canonical_482":
        query["yapo_ingestion.canonical_source_sha256"] = CANONICAL_SOURCE_SHA256
    elif marker == "live_37":
        query["yapo_ingestion.source_batch_id"] = LIVE_BATCH_ID
    else:
        raise RuntimeError(f"Refusing write to unrecognized Yapo lot for {document.get('listing_id')}.")
    return query


def _strip_paths(document: dict[str, Any], paths: set[str]) -> dict[str, Any]:
    result = deepcopy(document)
    for path in paths:
        parts = path.split(".")
        parent: Any = result
        for part in parts[:-1]:
            if not isinstance(parent, dict) or part not in parent:
                parent = None
                break
            parent = parent[part]
        if isinstance(parent, dict):
            parent.pop(parts[-1], None)
    return result


def _verify_only_allowlisted_changes(before: list[dict[str, Any]], after: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    before_by_id = {str(d.get("listing_id")): d for d in before}
    after_by_id = {str(d.get("listing_id")): d for d in after}
    if set(before_by_id) != set(after_by_id):
        raise RuntimeError("Readback listing IDs differ from the backed-up 519 IDs.")
    paths_by_id = {row["listing_id"]: set(row["updates"]) for row in rows}
    for listing_id in sorted(before_by_id):
        left = _strip_paths(before_by_id[listing_id], paths_by_id[listing_id])
        right = _strip_paths(after_by_id[listing_id], paths_by_id[listing_id])
        if json_util.dumps(left, json_options=CANONICAL_JSON_OPTIONS, sort_keys=True) != json_util.dumps(
            right, json_options=CANONICAL_JSON_OPTIONS, sort_keys=True
        ):
            raise RuntimeError(f"A field outside the allowlist changed on {listing_id}.")


def _verify_plan_readback(after: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[str]:
    by_id = {str(d.get("listing_id")): d for d in after}
    errors: list[str] = []
    for row in rows:
        listing_id = row["listing_id"]
        document = by_id.get(listing_id)
        if document is None:
            errors.append(f"{listing_id}: missing after write")
            continue
        for path, expected in row["updates"].items():
            if not _has_path(document, path) or not _values_equal(_get_path(document, path), expected):
                errors.append(f"{listing_id}: mismatch at {path}")
    if any((d.get("classification") or {}).get("assignment_ready") is True for d in after):
        errors.append("assignment_ready true after normalization")
    if any((d.get("classification") or {}).get("exclude_from_assignment") is not True for d in after):
        errors.append("exclude_from_assignment false/missing after normalization")
    if any((d.get("classification") or {}).get("owner_probability") not in (None, "") for d in after):
        errors.append("owner_probability present after normalization")
    if any(str(d.get("availability_status") or "").upper() == "ACTIVO" for d in after if _batch_marker(d) == "live_37"):
        errors.append("live record promoted to ACTIVO")
    return errors


def _apply_transaction(collection: Any, client: MongoClient, before: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    operations = [
        UpdateOne(_document_identity_filter(next(d for d in before if str(d.get("listing_id")) == row["listing_id"])), {"$set": row["updates"]})
        for row in rows if row["updates"]
    ]
    if not operations:
        return {"matched_count": 0, "modified_count": 0, "transaction": "no_changes"}

    with client.start_session() as session:
        with session.start_transaction():
            current = list(collection.find(_cohort_query(), session=session))
            if len(current) != 519 or _documents_signature(current) != _documents_signature(before):
                raise RuntimeError("Atlas snapshot changed after dry-run; transaction aborted before writes.")
            _assert_cohort_invariants(current)
            result = collection.bulk_write(operations, ordered=True, session=session)
            if result.matched_count != len(operations):
                raise RuntimeError(
                    f"Optimistic identity guard matched {result.matched_count}/{len(operations)} documents; transaction aborted."
                )
            after_transaction = list(collection.find(_cohort_query(), session=session))
            errors = _verify_plan_readback(after_transaction, rows)
            _verify_only_allowlisted_changes(before, after_transaction, rows)
            if errors:
                raise RuntimeError(f"Transactional readback failed; transaction aborted: {errors[:15]}.")
        return {
            "matched_count": int(result.matched_count),
            "modified_count": int(result.modified_count),
            "transaction": "committed",
        }


def run(*, apply: bool, confirm_target: str, backup_path: str = "", backup_sha256: str = "") -> dict[str, Any]:
    from scrapers.scraper_yapo.config import get_config

    config = get_config()
    if config.mongo_db != TARGET_DATABASE or config.mongo_collection != TARGET_COLLECTION:
        raise RuntimeError("Configured Mongo target must be URLS.propiedades_captacion.")
    if not (config.mongo_uri and re.search(r"mongodb(?:\+srv)?://", config.mongo_uri, re.I)):
        raise RuntimeError("MongoDB Atlas URI is missing or invalid.")
    if apply and confirm_target != f"{TARGET_DATABASE}.{TARGET_COLLECTION}":
        raise RuntimeError("--apply requires --confirm-target URLS.propiedades_captacion.")
    if apply and (not backup_path or not backup_sha256):
        raise RuntimeError("--apply requires the pre-write backup path and its SHA-256.")

    client = MongoClient(config.mongo_uri, serverSelectionTimeoutMS=20000, connectTimeoutMS=10000)
    try:
        client.admin.command("ping")
        database = client[TARGET_DATABASE]
        if TARGET_COLLECTION not in database.list_collection_names():
            raise RuntimeError("URLS.propiedades_captacion does not exist.")
        collection = database[TARGET_COLLECTION]
        documents = list(collection.find(_cohort_query()))
        invariants = _assert_cohort_invariants(documents)
        source_signature = _documents_signature(documents)
        rows, summary = _plan(collection, documents)
        if summary["v3_disagreements"] != 34:
            raise RuntimeError(
                "Expected the pre-normalization audit to contain exactly 34 V3 state mismatches; "
                f"found {summary['v3_disagreements']}."
            )
        summary["source_documents_sha256"] = source_signature
        summary["target"] = f"{TARGET_DATABASE}.{TARGET_COLLECTION}"
        summary["operations"] = {
            "new_scraping": False,
            "deepseek": False,
            "mongodb_writes_in_dry_run": False,
            "assignment_or_distribution": False,
            "notifications": False,
            "other_portals": False,
        }
        report_path = _report_path(config, "apply" if apply else "dryrun")

        if not apply:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out_backup = Path(REPO_ROOT / "backups" / f"yapo_519_pre_normalization_{stamp}.json")
            backup_hash = _backup_documents(documents, out_backup)
            report = {
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "mode": "dry_run",
                "summary": summary,
                "backup": {"path": str(out_backup), "sha256": backup_hash, "documents": len(documents)},
                "records": [
                    {key: value for key, value in row.items() if key not in {"updates"}}
                    for row in rows
                ],
                "exact_deltas": [
                    {"listing_id": row["listing_id"], "source_lot": row["source_lot"], "deltas": row["deltas"]}
                    for row in rows
                ],
                "preservation": {
                    "manual_observation_documents": sum(bool(d.get("manual_audit_observations")) for d in documents),
                    "manual_observation_entries": sum(len(d.get("manual_audit_observations") or []) for d in documents),
                    "pending_manual_review_documents": sum(bool(d.get("manual_review_batches")) for d in documents),
                    "pending_manual_review_entries": sum(len(d.get("manual_review_batches") or []) for d in documents),
                    "assignment_ready": 0,
                    "assigned": 0,
                },
            }
            report_hash = _write_report(report_path, report)
            return {
                "mode": "dry_run",
                "summary": summary,
                "backup_path": str(out_backup),
                "backup_sha256": backup_hash,
                "report_path": str(report_path),
                "report_sha256": report_hash,
                "updates": summary["total_dotted_field_updates"],
                "invariants": invariants,
            }

        saved_before = _load_backup(Path(backup_path), backup_sha256)
        if _documents_signature(saved_before) != source_signature:
            raise RuntimeError("Current 519 Atlas records do not match the verified pre-write backup.")
        # The full document hash and IDs must match, including CRM/manual audit
        # data, before even opening the write transaction.
        if {str(d.get("listing_id")) for d in saved_before} != {str(d.get("listing_id")) for d in documents}:
            raise RuntimeError("Backup IDs do not match the current 519-record cohort.")

        write_result = _apply_transaction(collection, client, documents, rows)
        after = list(collection.find(_cohort_query()))
        if len(after) != 519:
            raise RuntimeError(f"Post-commit cohort count changed: {len(after)}.")
        errors = _verify_plan_readback(after, rows)
        _verify_only_allowlisted_changes(saved_before, after, rows)
        if errors:
            raise RuntimeError(f"Post-commit Atlas readback failed: {errors[:20]}.")
        after_signature = _documents_signature(after)
        post_rows, post_summary = _plan(collection, after)
        remaining_updates = sum(bool(row["updates"]) for row in post_rows)
        if remaining_updates:
            pending_paths = sorted({path for row in post_rows for path in row["updates"]})
            raise RuntimeError(f"Normalization is not idempotent; remaining paths: {pending_paths}.")

        report = {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "mode": "applied_and_verified",
            "summary_before_write": summary,
            "summary_after_write": post_summary,
            "backup": {"path": backup_path, "sha256": backup_sha256, "documents": 519},
            "write": write_result,
            "verification": {
                "exact_cohort_519_ids": True,
                "all_planned_fields_match": True,
                "only_allowlisted_fields_changed": True,
                "idempotent_second_plan_updates": remaining_updates,
                "source_documents_sha256_before": source_signature,
                "source_documents_sha256_after": after_signature,
                "assignment_ready": 0,
                "assigned": 0,
                "manual_observations_preserved": True,
                "pending_manual_reviews_preserved": True,
                "notifications_or_distribution": False,
            },
        }
        report_hash = _write_report(report_path, report)
        return {
            "mode": "applied_and_verified",
            "summary": post_summary,
            "write": write_result,
            "report_path": str(report_path),
            "report_sha256": report_hash,
            "verification": report["verification"],
        }
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply $set after backup and approval arguments are verified.")
    parser.add_argument("--confirm-target", default="", help="Must equal URLS.propiedades_captacion for --apply.")
    parser.add_argument("--backup", default="", help="Exact pre-write snapshot created by dry-run.")
    parser.add_argument("--backup-sha256", default="", help="SHA-256 printed by the dry-run.")
    args = parser.parse_args()
    result = run(
        apply=args.apply,
        confirm_target=args.confirm_target,
        backup_path=args.backup,
        backup_sha256=args.backup_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
