"""Review the two approved Yapo cohorts for attributable commercial closing signatures.

This is a local-data/Mongo maintenance task, not a scraper. By default it only
audits and writes a dry-run report. ``--apply`` updates only eligible
classification fields for the exact 482 + 37 listing batches.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import json_util
from pymongo import MongoClient

from scrapers.scraper_yapo.classification_pipeline import CLASSIFIER_VERSION
from scrapers.scraper_yapo.classifier_rules import classify_yapo_listing, normalize_text
from scrapers.scraper_yapo.config import get_config
from scrapers.scraper_yapo.downloader import validate_html
from scrapers.scraper_yapo.extractor import extract_listing_fields
from scrapers.scraper_yapo.mongo_store import MongoStore

TARGET_DATABASE = "URLS"
TARGET_COLLECTION = "propiedades_captacion"
COHORT_BATCHES = {
    "maipu_houses_3000_6000_20261103": 482,
    "issue122_maipu_20261104_141023_35cf": 37,
}
TARGET_TOTAL = sum(COHORT_BATCHES.values())
RECLASSIFIABLE_STATES = {"INCIERTO", "DUEÑO_PROBABLE"}
TARGET_STATE = "CORREDOR_SEGURO"
TARGET_CANONICAL_STATE = "BROKER_CONFIRMED"

CLASSIFICATION_FIELDS = {
    "state", "classification_state", "final", "final_state", "identity_classification",
    "source", "reason", "classification_reason", "final_evidence", "final_reason",
    "evidence", "evidence_strength", "classification_rule_ids", "owner_evidence",
    "broker_evidence", "contradictions", "missing_information", "owner_signal_candidates",
    "owner_review_priority_signals", "owner_signal_evidence", "owner_classification_confirmed",
    "family_or_representative_signals", "direct_deal_with_commercial_contradictions",
    "company_like_suspected", "company_like_evidence", "contextual_analysis",
    "profile_identity_signal", "classifier_version", "classification_route",
}
PRESERVED_CLASSIFICATION_FIELDS = {
    "assignment_ready", "exclude_from_assignment", "assignment_block_reasons",
    "manual_review_required", "manual_review_reason", "confidence", "confidence_type",
    "canonical_classification_version", "classification_serialization_version",
    "deepseek_call_executed",
}


def _state(value: Any) -> str:
    return str(value or "").strip().upper().replace("DUENO", "DUEÑO")


def _is_human_confirmed(document: dict[str, Any]) -> bool:
    return any(
        MongoStore._human_confirmed_classification(document.get(field))
        for field in ("classification", "classification_v3")
    )


def _candidate_status(document: dict[str, Any], result: dict[str, Any] | None) -> tuple[bool, str]:
    classification = document.get("classification") or {}
    current_state = _state(classification.get("state"))
    if current_state not in RECLASSIFIABLE_STATES:
        return False, "current_state_not_uncertain_or_owner_probable"
    if _is_human_confirmed(document):
        return False, "human_confirmed_classification_preserved"
    if classification.get("assignment_ready") is not False or classification.get("exclude_from_assignment") is not True:
        return False, "assignment_guard_not_locked"
    if document.get("exclude_from_assignment") is not True:
        return False, "root_assignment_exclusion_not_locked"
    if not result or result.get("state") != TARGET_STATE:
        return False, "no_new_safe_broker_classification"
    if result.get("source") != "publisher_linked_commercial_closing_signature":
        return False, "classification_not_from_new_closing_signature_rule"
    return True, "eligible"


def _path_value(document: dict[str, Any], *paths: str) -> str:
    for path in paths:
        value: Any = document
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if str(value or "").strip():
            return str(value)
    return ""


def _needs_saved_html_for_commercial_audit(document: dict[str, Any]) -> bool:
    """Use local HTML only when stored text contains a plausible relevant clue."""
    details = document.get("details") if isinstance(document.get("details"), dict) else {}
    description = max(
        (str(document.get(key) or details.get(key) or "") for key in ("description", "descripcion")),
        key=len,
        default="",
    )
    body_text = str(document.get("body_text") or details.get("body_text") or "")
    normalized_body = normalize_text(body_text)
    if "mas anuncios de" in normalized_body:
        return True
    normalized_tail = normalize_text(description[-420:])
    business_marker = any(term in normalized_tail for term in (
        "propiedades", "inmobiliaria", "corredora", "corretaje", "bienes raices", "real estate",
    ))
    contact_channel = any(term in normalized_tail for term in (
        "whatsapp", "telefono", "celular", "fono", "correo", "email", "instagram", "llamar",
    ))
    publisher_tokens = normalize_text(str(document.get("publicador_visible") or document.get("contact_name") or "")).split()
    identity_in_tail = len(publisher_tokens) >= 2 and publisher_tokens[0] in normalized_tail.split() and publisher_tokens[-1] in normalized_tail.split()
    return bool(business_marker and contact_channel and identity_in_tail)


def _classification_input(document: dict[str, Any], *, parse_local_html: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build classifier input from stored fields and optional validated local HTML only."""
    extracted = deepcopy(document)
    details = document.get("details") if isinstance(document.get("details"), dict) else {}
    for field in (
        "title", "description", "descripcion", "publicador_visible", "contact_name",
        "seller_type", "seller_profile_id", "publisher_field_provenance",
    ):
        if not extracted.get(field) and details.get(field):
            extracted[field] = details[field]
    url = _path_value(document, "url", "canonical_url", "details.source_url")
    html_path = _path_value(document, "html_path", "html_source_path", "details.html_path")
    html_info: dict[str, Any] = {"path": html_path, "status": "not_needed_for_current_evidence", "parsed": False}
    local_path = Path(html_path) if html_path else None
    if parse_local_html and local_path and local_path.is_file():
        try:
            html = local_path.read_text(encoding="utf-8", errors="strict")
            validation = validate_html(html)
            html_info = {"path": str(local_path), **validation, "parsed": False}
            if validation.get("status") == "OK":
                parsed = extract_listing_fields(html, url, include_images=False, include_listing_details=True)
                # Local extraction may enrich provenance but is never written back
                # into listing facts during this targeted classification repair.
                for field in (
                    "publicador_visible", "contact_name", "seller_type", "seller_profile_id",
                    "publisher_field_provenance", "publisher_more_ads",
                ):
                    if parsed.get(field) not in (None, "", [], {}):
                        extracted[field] = parsed[field]
                stored_descriptions = [
                    str(document.get(key) or "") for key in ("description", "descripcion")
                ] + [str(details.get(key) or "") for key in ("description", "descripcion")]
                stored_description = max(stored_descriptions, key=len, default="")
                parsed_description = str(parsed.get("description") or "")
                full_description = max((stored_description, parsed_description), key=len)
                if full_description:
                    extracted["description"] = full_description
                    extracted["descripcion"] = full_description
                html_info["parsed"] = True
                html_info["more_ads_count"] = int((extracted.get("publisher_more_ads") or {}).get("distinct_listing_count") or 0)
        except (OSError, UnicodeError, ValueError) as exc:
            html_info = {"path": html_path, "status": "local_read_error", "error": type(exc).__name__, "parsed": False}
    if parse_local_html and not local_path:
        html_info = {"path": "", "status": "missing", "parsed": False}
    elif parse_local_html and local_path and not local_path.is_file():
        html_info = {"path": html_path, "status": "missing", "parsed": False}
    extracted.setdefault("publisher_profile_context", document.get("publisher_profile_context") or {})
    extracted.setdefault("listing_id", str(document.get("listing_id") or ""))
    if document.get("html_validation_status"):
        extracted.setdefault("html_validation_status", document["html_validation_status"])
    return extracted, html_info


def _classification_set(result: dict[str, Any]) -> dict[str, Any]:
    evidence = list(result.get("evidence") or [])
    values = {
        "state": TARGET_STATE,
        "classification_state": TARGET_STATE,
        "final": TARGET_CANONICAL_STATE,
        "final_state": TARGET_STATE,
        "identity_classification": TARGET_CANONICAL_STATE,
        "source": "publisher_linked_commercial_closing_signature",
        "reason": str(result.get("reason") or ""),
        "classification_reason": str(result.get("reason") or ""),
        "final_reason": str(result.get("reason") or ""),
        "evidence": evidence,
        "final_evidence": evidence,
        "evidence_strength": str(result.get("evidence_strength") or "ALTA"),
        "classification_rule_ids": list(result.get("classification_rule_ids") or []),
        "owner_evidence": list(result.get("owner_evidence") or []),
        "broker_evidence": list(result.get("broker_evidence") or []),
        "contradictions": list(result.get("contradictions") or []),
        "missing_information": list(result.get("missing_information") or []),
        "owner_signal_candidates": list(result.get("owner_signal_candidates") or []),
        "owner_review_priority_signals": list(result.get("owner_review_priority_signals") or []),
        "owner_signal_evidence": list(result.get("owner_signal_evidence") or []),
        "owner_classification_confirmed": bool(result.get("owner_classification_confirmed", False)),
        "family_or_representative_signals": list(result.get("family_or_representative_signals") or []),
        "direct_deal_with_commercial_contradictions": bool(result.get("direct_deal_with_commercial_contradictions")),
        "company_like_suspected": bool(result.get("company_like_suspected")),
        "company_like_evidence": list(result.get("company_like_evidence") or []),
        "contextual_analysis": dict(result.get("contextual_analysis") or {}),
        "profile_identity_signal": bool(result.get("profile_identity_signal")),
        "classifier_version": CLASSIFIER_VERSION,
        "classification_route": "deterministic_only",
    }
    return values


def _update_document(document: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    values = _classification_set(result)
    set_fields: dict[str, Any] = {f"classification.{key}": value for key, value in values.items()}
    set_fields.update({f"classification_v3.{key}": value for key, value in values.items()})
    set_fields["classification_evidence"] = values["evidence"]
    calculated_at = datetime.now(timezone.utc).isoformat()
    set_fields["classification_v3.calculated_at"] = calculated_at
    # Both assignment barriers and all operational/CRM values stay exactly as
    # stored. The preflight requires the assignment barrier to already be shut.
    return set_fields


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_hash(value: Any) -> str:
    return hashlib.sha256(json_util.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _audit_collection(collection: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    query = {"origen": "yapo", "batch_id": {"$in": list(COHORT_BATCHES)}}
    documents = list(collection.find(query).sort("listing_id", 1))
    batch_counts: dict[str, int] = {}
    for batch_id, expected_count in COHORT_BATCHES.items():
        count = sum(document.get("batch_id") == batch_id for document in documents)
        batch_counts[batch_id] = count
        if count != expected_count:
            raise RuntimeError(f"Expected {expected_count} records for {batch_id}; found {count}.")
    ids = [str(document.get("listing_id") or "").strip() for document in documents]
    if len(documents) != TARGET_TOTAL or any(not value for value in ids) or len(set(ids)) != TARGET_TOTAL:
        raise RuntimeError(f"The exact 519-record cohort is not unique and complete: documents={len(documents)}, unique_ids={len(set(ids))}.")

    report_rows: list[dict[str, Any]] = []
    planned: list[dict[str, Any]] = []
    for document in documents:
        parse_local_html = _needs_saved_html_for_commercial_audit(document)
        extracted, html_info = _classification_input(document, parse_local_html=parse_local_html)
        result = classify_yapo_listing(extracted)
        eligible, status = _candidate_status(document, result)
        current_classification = document.get("classification") or {}
        row = {
            "listing_id": str(document.get("listing_id") or ""),
            "batch_id": document.get("batch_id"),
            "before": _state(current_classification.get("state")),
            "v3_before": _state((document.get("classification_v3") or {}).get("state")),
            "proposed": result.get("state") if result else "UNAVAILABLE",
            "source": result.get("source") if result else "",
            "result_reason": result.get("reason") if result else "",
            "evidence": list(result.get("evidence") or []) if result else [],
            "publisher_more_ads_count": int((result or {}).get("contextual_analysis", {}).get("publisher_more_ads_count") or 0),
            "html": html_info,
            "decision": status,
            "eligible": eligible,
            "human_confirmed": _is_human_confirmed(document),
        }
        report_rows.append(row)
        if eligible:
            planned.append({"document": document, "result": result, "row": row})
    return documents, planned, {"batch_counts": batch_counts, "report_rows": report_rows}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_util.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def run(*, apply: bool = False, root_dir: Path | None = None) -> dict[str, Any]:
    config = get_config()
    if not config.mongo_uri:
        raise RuntimeError("MONGO_URI is not configured.")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = root_dir or Path(__file__).resolve().parents[2]
    client = MongoClient(config.mongo_uri, serverSelectionTimeoutMS=20000, connectTimeoutMS=10000)
    try:
        collection = client[TARGET_DATABASE][TARGET_COLLECTION]
        documents, planned, audit = _audit_collection(collection)
        counts = {
            "scanned": len(documents),
            "html_parsed": sum(row["html"].get("parsed") is True for row in audit["report_rows"]),
            "html_checked": sum(row["html"].get("status") != "not_needed_for_current_evidence" for row in audit["report_rows"]),
            "html_missing_or_unusable": sum(row["html"].get("status") in {"missing", "INVALID", "BLOCKED", "LISTING_REMOVED", "local_read_error"} for row in audit["report_rows"]),
            "html_not_needed": sum(row["html"].get("status") == "not_needed_for_current_evidence" for row in audit["report_rows"]),
            "new_safe_broker_candidates": len(planned),
            "human_confirmed_skipped": sum(row["decision"] == "human_confirmed_classification_preserved" for row in audit["report_rows"]),
        }
        backup_path = None
        backup_sha256 = None
        applied_ids: list[str] = []
        if apply and planned:
            backup_path = root / "backups" / f"yapo_519_commercial_signature_before_{timestamp}.json"
            before_documents = [item["document"] for item in planned]
            _write_json(backup_path, before_documents)
            backup_sha256 = _file_hash(backup_path)
            if len(json_util.loads(backup_path.read_text(encoding="utf-8"))) != len(planned):
                raise RuntimeError("Pre-update backup record count does not match the planned update set.")
            for item in planned:
                document = item["document"]
                listing_id = str(document.get("listing_id") or "")
                old_state = _state((document.get("classification") or {}).get("state"))
                guard = {
                    "_id": document["_id"], "origen": "yapo", "batch_id": document.get("batch_id"),
                    "listing_id": listing_id, "classification.state": old_state,
                    "classification_v3.state": _state((document.get("classification_v3") or {}).get("state")),
                    "classification.assignment_ready": False,
                    "classification.exclude_from_assignment": True,
                    "exclude_from_assignment": True,
                    "classification.human_confirmed": {"$ne": True},
                    "classification.confirmed_by_human": {"$ne": True},
                    "classification.manual_confirmation": {"$ne": True},
                    "classification.source": {"$nin": ["human", "manual", "crm_manual", "human_review"]},
                    "classification.confirmed_by": {"$nin": ["human", "manual", "crm_manual", "human_review"]},
                    "classification_v3.human_confirmed": {"$ne": True},
                    "classification_v3.confirmed_by_human": {"$ne": True},
                    "classification_v3.manual_confirmation": {"$ne": True},
                    "classification_v3.source": {"$nin": ["human", "manual", "crm_manual", "human_review"]},
                    "classification_v3.confirmed_by": {"$nin": ["human", "manual", "crm_manual", "human_review"]},
                }
                res = collection.update_one(guard, {"$set": _update_document(document, item["result"])}, upsert=False)
                if res.matched_count == 1:
                    applied_ids.append(listing_id)
            if len(applied_ids) != len(planned):
                raise RuntimeError(
                    f"Safe update guard matched {len(applied_ids)} of {len(planned)} planned records. "
                    f"Backup saved at {backup_path}; verify the partial set before continuing."
                )

            preserved_checks = tuple(PRESERVED_CLASSIFICATION_FIELDS)
            for item in planned:
                before = item["document"]
                after = collection.find_one({"_id": before["_id"]})
                if after is None:
                    raise RuntimeError(f"Updated listing {before.get('listing_id')} disappeared on readback.")
                expected_values = _classification_set(item["result"])
                for field in ("classification", "classification_v3"):
                    after_classification = after.get(field) or {}
                    for name in ("state", "classification_state", "final_state", "source", "reason", "classification_reason", "final_reason", "evidence", "final_evidence"):
                        if after_classification.get(name) != expected_values[name]:
                            raise RuntimeError(f"Classification readback mismatch for {before.get('listing_id')}: {field}.{name}.")
                    if after_classification.get("final") != TARGET_CANONICAL_STATE:
                        raise RuntimeError(f"Canonical final state mismatch for {before.get('listing_id')}: {field}.final.")
                for field in ("availability_status", "availability_source", "availability_evidence", "assignment_ready", "exclude_from_assignment", "gestion", "asignacion", "asignaciones", "assigned_to", "assigned_executive", "classification_original"):
                    if before.get(field) != after.get(field):
                        raise RuntimeError(f"Protected CRM/operational field changed for {before.get('listing_id')}: {field}.")
                for field in preserved_checks:
                    if (before.get("classification") or {}).get(field) != (after.get("classification") or {}).get(field):
                        raise RuntimeError(f"Assignment/manual-review barrier changed for {before.get('listing_id')}: {field}.")
                    if (before.get("classification_v3") or {}).get(field) != (after.get("classification_v3") or {}).get(field):
                        raise RuntimeError(f"V3 assignment/manual-review barrier changed for {before.get('listing_id')}: {field}.")
                for field in set(before) | set(after):
                    if field not in {"classification", "classification_v3", "classification_evidence"} and before.get(field) != after.get(field):
                        raise RuntimeError(f"Unapproved document field changed for {before.get('listing_id')}: {field}.")
                for field in ("classification", "classification_v3"):
                    before_class = before.get(field) or {}
                    after_class = after.get(field) or {}
                    allowed = CLASSIFICATION_FIELDS | ({"calculated_at"} if field == "classification_v3" else set())
                    before_unmodified = {key: value for key, value in before_class.items() if key not in allowed}
                    after_unmodified = {key: value for key, value in after_class.items() if key not in allowed}
                    if before_unmodified != after_unmodified:
                        raise RuntimeError(f"Unapproved nested classification field changed for {before.get('listing_id')}: {field}.")
                if after.get("classification_evidence") != expected_values["evidence"]:
                    raise RuntimeError(f"Root classification evidence mismatch for {before.get('listing_id')}.")
                if _is_human_confirmed(after):
                    raise RuntimeError(f"Human-confirmed classification appeared during update for {before.get('listing_id')}.")
                if collection.count_documents({"origen": "yapo", "listing_id": str(before.get("listing_id"))}) != 1:
                    raise RuntimeError(f"Yapo identity is not unique after update for {before.get('listing_id')}.")

        report = {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "database": TARGET_DATABASE,
            "collection": TARGET_COLLECTION,
            "scope": {"batches": COHORT_BATCHES, "expected_records": TARGET_TOTAL},
            "mode": "apply" if apply else "dry_run",
            "counts": counts,
            "backup": {"path": str(backup_path) if backup_path else "", "sha256": backup_sha256 or ""},
            "applied_listing_ids": applied_ids,
            "distribution_invoked": False,
            "scraping_or_network_requests": False,
            "deepseek_calls": 0,
            "classification_preservation": {
                "manual_confirmed_skipped": counts["human_confirmed_skipped"],
                "assignment_ready_preserved_false": True,
                "exclude_from_assignment_preserved_true": True,
                "availability_and_crm_preserved": True,
            },
            **audit,
        }
        report_path = root / "reports" / f"yapo_519_commercial_signature_{timestamp}.json"
        _write_json(report_path, report)
        report["report_path"] = str(report_path)
        return report
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply guarded classification-only updates after creating a backup.")
    args = parser.parse_args()
    report = run(apply=args.apply)
    print(json.dumps({
        "mode": report["mode"], "counts": report["counts"],
        "batch_counts": report["batch_counts"], "backup": report["backup"],
        "applied_listing_ids": report["applied_listing_ids"], "report_path": report["report_path"],
        "distribution_invoked": report["distribution_invoked"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
