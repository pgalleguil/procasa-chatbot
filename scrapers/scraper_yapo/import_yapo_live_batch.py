"""Finish and migrate a saved Yapo live batch to the operational collection.

Uses saved HTML, verifies exact publisher profiles through the configured
FlashProxy endpoint, writes via the existing protected importer, and never
invokes assignments or post-scrape distribution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scrapers.scraper_yapo.classification_pipeline import CLASSIFIER_VERSION
from scrapers.scraper_yapo.config import get_config
from scrapers.scraper_yapo.import_yapo_prepared_batch import (
    TARGET_COLLECTION,
    TARGET_DATABASE,
    _backup_matching_documents,
    _field_schema_conflicts,
    _mongo_import_payload,
    _read_preflight,
    _top_level_type_sets,
    _upsert_one,
    _verify_import_readback,
)
from scrapers.scraper_yapo.mongo_store import MongoStore
from scrapers.scraper_yapo.pilot import _apply_yapo_availability_hold, _reclassify_batch_by_exact_profile
from scrapers.scraper_yapo.publisher_profile import fetch_publisher_profile_history
from scrapers.scraper_yapo.proxy_manager import ProxyManager
from scrapers.scraper_yapo.staging import (
    SOURCE_PORTAL,
    STAGING_COLLECTION,
    STAGING_DATABASE,
)

EXPECTED_NEW_BATCH_SIZE = 37
ALLOWED_STATES = {"CORREDOR_SEGURO", "CORREDOR_PROBABLE", "DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"}


def _assert_atlas_flashproxy_only(config: Any) -> None:
    if config.mongo_db != STAGING_DATABASE or config.mongo_collection != TARGET_COLLECTION:
        raise RuntimeError("Mongo config does not target URLS.propiedades_captacion.")
    if not (urlsplit(config.mongo_uri).hostname or "").endswith("mongodb.net"):
        raise RuntimeError("Mongo target is not Atlas; no profile check or write began.")
    manager = ProxyManager.from_config(config)
    if manager.provider != "flashproxy" or len(manager._proxies) != 1:
        raise RuntimeError("Expected one configured FlashProxy endpoint; no Yapo request began.")
    proxy = urlsplit(manager._proxies[0])
    host = (proxy.hostname or "").lower().rstrip(".")
    if host != "flashproxy.io" and not host.endswith(".flashproxy.io"):
        raise RuntimeError("Configured endpoint is not FlashProxy; no Yapo request began.")
    if not proxy.username or not proxy.password:
        raise RuntimeError("FlashProxy credentials are missing; no Yapo request began.")


def _profile_enrich(records: list[dict[str, Any]], config: Any, batch_id: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        profile_id = str(record.get("seller_profile_id") or "").strip()
        if profile_id:
            groups[profile_id].append(record)
    limit = max(0, min(int(config.yapo_profile_max_profiles or 12), 50))
    ordered = sorted(groups, key=lambda profile_id: (-len(groups[profile_id]), profile_id))
    contexts: dict[str, dict[str, Any]] = {}
    errors = 0
    for profile_id in ordered[:limit]:
        first = groups[profile_id][0]
        profile_url = str(first.get("seller_profile_url") or f"/user/profile/id/{profile_id}")
        try:
            contexts[profile_id] = fetch_publisher_profile_history(
                profile_id,
                profile_url,
                config,
                batch_id=batch_id,
            )
        except Exception as exc:
            errors += 1
            contexts[profile_id] = {
                "seller_profile_id": profile_id,
                "seller_profile_url": profile_url,
                "status": "NO_VERIFICADO",
                "reason": f"flashproxy_profile_request_failed:{type(exc).__name__}",
                "observed_listing_count": 0,
                "observed_listing_ids": [],
                "profile_enumeration_complete": False,
                "enumeration_scope": "not_verified",
            }
    for profile_id, items in groups.items():
        context = contexts.get(profile_id, {
            "seller_profile_id": profile_id,
            "status": "NO_VERIFICADO",
            "reason": "profile_request_limit_reached",
            "observed_listing_count": 0,
            "observed_listing_ids": [],
            "profile_enumeration_complete": False,
            "enumeration_scope": "not_requested",
        })
        for record in items:
            existing = dict(record.get("publisher_profile_context") or {})
            record["publisher_profile_context"] = {**existing, **context}
            details = record.get("details")
            if isinstance(details, dict):
                record["details"] = {**details, "publisher_profile_context": dict(record["publisher_profile_context"])}
    return {
        "distinct_profile_ids": len(groups),
        "profile_requests_attempted": len(contexts),
        "profiles_observed": sum(context.get("status") == "OBSERVED" for context in contexts.values()),
        "profiles_complete": sum(bool(context.get("profile_enumeration_complete")) for context in contexts.values()),
        "not_requested_due_to_profile_cap": max(0, len(groups) - len(contexts)),
        "request_errors": errors,
        "source": "FlashProxy only; saved page cache reused when available",
    }


def _finalize_record(record: dict[str, Any]) -> dict[str, Any]:
    record = deepcopy(record)
    state = str((record.get("classification") or {}).get("state") or "").upper()
    html_status = str(record.get("html_validation_status") or "").upper()
    issues = list(record.get("extraction_issues") or [])

    if state not in ALLOWED_STATES:
        record["classification"] = {
            "state": "INCIERTO",
            "reason": "La ficha no entregó evidencia completa para clasificar al anunciante.",
            "evidence": [],
            "source": "technical_validation",
            "requires_review": True,
            "manual_review_required": True,
        }
        state = "INCIERTO"

    if html_status == "LISTING_REMOVED" or "html_listing_removed" in issues:
        record["html_validation_status"] = "LISTING_REMOVED"
        record["scrape_stage"] = "ad_removed"
        record["availability_status"] = "DESACTIVADO"
        record.setdefault("availability_source", "flashproxy")
    elif html_status != "OK":
        record["html_validation_status"] = html_status or "NO_VERIFICADO"
        record["scrape_stage"] = "needs_rescrape"
        record["availability_status"] = "NO_VERIFICADO"
        record.setdefault("availability_source", "flashproxy_request_failed")
        if not any(str(item).startswith("detail_error:") for item in issues):
            issues.append("detail_html_not_verified")
    else:
        record["scrape_stage"] = "processed"

    classification = dict(record.get("classification") or {})
    classification["assignment_ready"] = False
    classification["exclude_from_assignment"] = True
    classification["manual_review_required"] = (
        classification.get("manual_review_required") is True
        or record.get("availability_status") != "ACTIVO"
        or state == "INCIERTO"
    )
    blockers = set(classification.get("assignment_block_reasons") or [])
    if record.get("availability_status") != "ACTIVO":
        blockers.add("yapo_availability_not_confirmed")
    classification["assignment_block_reasons"] = sorted(blockers)
    if classification.get("owner_probability") is not None:
        raise RuntimeError(f"Yapo listing {record.get('listing_id')} has a probability field; migration blocked.")
    record["classification"] = classification
    record["classification_evidence"] = classification.get("evidence") or record.get("classification_evidence") or []
    record["classification_v3"] = {
        **classification,
        "classifier_version": CLASSIFIER_VERSION,
        "deepseek_call_executed": False,
        "calculated_at": datetime.now(timezone.utc).isoformat(),
    }
    record["classification_debug"] = {
        **(record.get("classification_debug") if isinstance(record.get("classification_debug"), dict) else {}),
        "deepseek_enabled": False,
        "deepseek_call_executed": False,
        "assignment_or_distribution_executed": False,
    }
    record["extraction_issues"] = issues
    record["assignment_ready"] = False
    record["distribution_blocked"] = True
    record["crm_promotion_allowed"] = False
    record["staging_only"] = True
    return record


def _assert_no_identity_conflicts(records: list[dict[str, Any]], config: Any) -> dict[str, Any]:
    per_record, schema = _read_preflight(records, config)
    identity_counts = Counter(str(row.get("identity_status")) for row in per_record)
    conflicts = [row for row in per_record if row.get("identity_status") == "IDENTITY_CONFLICT"]
    schema_conflicts = [
        row for row in per_record
        if row.get("field_type_incompatibilities") or row.get("schema_type_incompatibilities")
    ]
    if conflicts or schema_conflicts:
        raise RuntimeError(
            f"New batch blocked before write: {len(conflicts)} identity conflicts, "
            f"{len(schema_conflicts)} schema/type conflicts."
        )
    if any(int((row.get("classification") or {}).get("assignment_ready") is True) for row in records):
        raise RuntimeError("New batch contains assignment-ready records; main import blocked.")
    if any((row.get("classification") or {}).get("owner_probability") is not None for row in records):
        raise RuntimeError("New batch has non-null owner_probability; main import blocked.")
    return {
        "identity_counts": dict(identity_counts),
        "identity_conflicts": len(conflicts),
        "schema_type_conflicts": len(schema_conflicts),
        "storage_exceptions": sum(bool(row.get("storage_exceptions")) for row in per_record),
        "assignment_ready": 0,
        "mongo_collection_documents": schema.get("collection_documents"),
        "unique_index": "origen+listing_id (verified by main importer)",
    }


def _staging_drop_snapshot(staging: Any, expected_count: int) -> tuple[list[dict[str, Any]], set[str]]:
    """Require the entire temporary collection to be exactly the verified Yapo set."""
    total_count = int(staging.count_documents({}))
    records = list(staging.find({"source_portal": SOURCE_PORTAL}))
    listing_ids = [str(record.get("listing_id") or "") for record in records]
    if (
        total_count != expected_count
        or len(records) != expected_count
        or any(not listing_id for listing_id in listing_ids)
        or len(set(listing_ids)) != expected_count
    ):
        raise RuntimeError(
            "Stage removal blocked: temporary collection must contain exactly "
            f"{expected_count} unique Yapo records (found total={total_count}, Yapo={len(records)})."
        )
    return records, set(listing_ids)


def run(batch_id: str, *, finish_profiles: bool, apply: bool, confirm_target: str,
        drop_staging_after_verify: bool, confirm_drop_stage: str,
        expected_new_batch_size: int = EXPECTED_NEW_BATCH_SIZE,
        expected_stage_count: int | None = None) -> dict[str, Any]:
    config = get_config()
    _assert_atlas_flashproxy_only(config)
    if apply and confirm_target != f"{TARGET_DATABASE}.{TARGET_COLLECTION}":
        raise RuntimeError("Main import requires --confirm-target URLS.propiedades_captacion.")
    if drop_staging_after_verify and confirm_drop_stage != f"{STAGING_DATABASE}.{STAGING_COLLECTION}":
        raise RuntimeError("Stage removal requires --confirm-drop-stage URLS.yapo_preimportacion.")

    from pymongo import MongoClient
    from bson import json_util

    client = MongoClient(config.mongo_uri, serverSelectionTimeoutMS=20000, connectTimeoutMS=10000)
    output_dir = config.reports_dir / "issue122_maipu"
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        client.admin.command("ping")
        database = client[STAGING_DATABASE]
        if TARGET_COLLECTION not in database.list_collection_names() or STAGING_COLLECTION not in database.list_collection_names():
            raise RuntimeError("Operational or staging collection missing.")
        staging = database[STAGING_COLLECTION]
        records = list(staging.find(
            {"source_portal": SOURCE_PORTAL, "staging_batch_id": batch_id},
            {"_id": 0},
        ).sort("listing_id", 1))
        expected_new_batch_size = max(0, int(expected_new_batch_size))
        if expected_new_batch_size == 0 or len(records) != expected_new_batch_size:
            raise RuntimeError(f"Expected {expected_new_batch_size} saved live records; found {len(records)}.")
        ids = [str(record.get("listing_id") or "") for record in records]
        if len(set(ids)) != expected_new_batch_size or any(not value for value in ids):
            raise RuntimeError("New batch has missing or duplicate IDs.")
        normalized_urls = [MongoStore.normalize_listing_url(str(record.get("url") or record.get("canonical_url") or "")) for record in records]
        if any(not value for value in normalized_urls) or len(set(normalized_urls)) != expected_new_batch_size:
            raise RuntimeError("New batch has missing, invalid, or duplicate Yapo URLs.")

        profile_already_finished = bool(records) and all(record.get("profile_enrichment_finished") is True for record in records)
        profile_report = {"not_run": not finish_profiles, "finished_in_previous_step": profile_already_finished}
        if finish_profiles and not profile_already_finished:
            profile_report = _profile_enrich(records, config, batch_id)
        elif profile_already_finished:
            profile_report = {"finished_in_previous_step": True, "new_profile_requests": 0}
        _reclassify_batch_by_exact_profile(records)
        finalized = [_finalize_record(record) for record in records]

        # Persist the final, offline/profile-enriched state to staging first so
        # recovery remains possible if the operational write is interrupted.
        for record in finalized:
            staging.update_one(
                {"source_portal": SOURCE_PORTAL, "listing_id": str(record["listing_id"]), "staging_batch_id": batch_id},
                {"$set": {
                    "classification": record["classification"],
                    "classification_v3": record["classification_v3"],
                    "classification_evidence": record["classification_evidence"],
                    "classification_debug": record["classification_debug"],
                    "publisher_profile_context": record.get("publisher_profile_context") or {},
                    "details": record.get("details") or {},
                    "availability_status": record.get("availability_status") or "NO_VERIFICADO",
                    "availability_source": record.get("availability_source") or "not_verified",
                    "availability_checked_at": record.get("availability_checked_at") or "",
                    "availability_evidence": record.get("availability_evidence") or [],
                    "html_validation_status": record.get("html_validation_status") or "NO_VERIFICADO",
                    "scrape_stage": record.get("scrape_stage") or "needs_rescrape",
                    "extraction_issues": record.get("extraction_issues") or [],
                    "filter_status": record.get("filter_status") or "PENDIENTE_VALIDACION",
                    "filter_validation": record.get("filter_validation") or {},
                    "assignment_ready": False,
                    "distribution_blocked": True,
                    "crm_promotion_allowed": False,
                    "staging_only": True,
                    "profile_enrichment_finished": bool(finish_profiles or record.get("profile_enrichment_finished")),
                }},
            )

        identity_preflight = _assert_no_identity_conflicts(finalized, config)
        type_sets = _top_level_type_sets(database[TARGET_COLLECTION])
        payload_conflicts = [
            {"listing_id": str(record.get("listing_id")), "field": issue["field"]}
            for record in finalized
            for issue in _field_schema_conflicts(_mongo_import_payload(record), type_sets)
        ]
        if payload_conflicts:
            raise RuntimeError(f"New batch blocked by {len(payload_conflicts)} Mongo field type conflicts.")

        backup_path = None
        write_results = []
        verification = {"not_run": True}
        if apply:
            main = database[TARGET_COLLECTION]
            batch_hash = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()
            backup_path = _backup_matching_documents(main, finalized, output_dir, batch_hash)
            for record in finalized:
                write_results.append(_upsert_one(main, MongoStore(config), record))
            verification = _verify_import_readback(main, finalized, MongoStore(config))
            if not verification.get("passed"):
                raise RuntimeError("New live batch failed post-write readback verification; staging retained.")

        export_path = output_dir / f"{batch_id}_records.json"
        export_path.write_text(json_util.dumps(finalized, ensure_ascii=False, indent=2), encoding="utf-8")
        class_counts = Counter(str((record.get("classification") or {}).get("state") or "INCIERTO") for record in finalized)
        availability_counts = Counter(str(record.get("availability_status") or "NO_VERIFICADO") for record in finalized)
        filter_counts = Counter(str(record.get("filter_status") or "PENDIENTE_VALIDACION") for record in finalized)
        usable = [
            record for record in finalized
            if record.get("availability_status") == "ACTIVO"
            and str((record.get("classification") or {}).get("state") or "") in {"DUEÑO_SEGURO", "DUEÑO_PROBABLE"}
            and record.get("filter_status") == "PASA"
            and bool(record.get("contact_phone") or record.get("contact_email"))
            and record.get("html_validation_status") == "OK"
            and bool(str(record.get("description") or "").strip())
        ]
        result = {
            "batch_id": batch_id,
            "new_records": len(finalized),
            "classification_counts": dict(class_counts),
            "availability_counts": dict(availability_counts),
            "filter_counts": dict(filter_counts),
            "profile_enrichment": profile_report,
            "identity_preflight": identity_preflight,
            "schema_type_conflicts": len(payload_conflicts),
            "new_records_written_to_operational": sum(bool(item.get("upserted_id")) for item in write_results),
            "new_records_already_existing": sum(int(item.get("matched_count", 0)) > 0 for item in write_results),
            "post_write_verification": verification,
            "usable_for_assignment_now": len(usable),
            "assignment_authorized": 0,
            "distribution_runs": 0,
            "deepseek_calls": 0,
            "yapo_search_pages_requested": 0,
            "backup_path": str(backup_path) if backup_path else "",
            "record_export_path": str(export_path),
        }

        if drop_staging_after_verify:
            # Do not drop until every staging ID is verified in the main
            # collection and an exact BSON backup of staging is durable.
            expected_stage_count = int(expected_stage_count if expected_stage_count is not None else 519)
            all_staged, source_ids = _staging_drop_snapshot(staging, expected_stage_count)
            target = database[TARGET_COLLECTION]
            main_rows = list(target.find(
                {"origen": SOURCE_PORTAL, "listing_id": {"$in": sorted(source_ids)}},
                {"listing_id": 1, "yapo_ingestion.source_collection": 1,
                 "yapo_ingestion.source_batch_id": 1, "_id": 0},
            ))
            main_ids = {str(doc.get("listing_id") or "") for doc in main_rows}
            if main_ids != source_ids or len(main_rows) != 519:
                raise RuntimeError("Stage cleanup blocked: target collection does not contain exactly all 519 IDs.")
            stage_backup = output_dir / f"staging_{expected_stage_count}_before_drop_{batch_id}.json"
            stage_backup.write_text(json_util.dumps({
                "database": STAGING_DATABASE,
                "collection": STAGING_COLLECTION,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "documents": all_staged,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            database.drop_collection(STAGING_COLLECTION)
            if STAGING_COLLECTION in database.list_collection_names():
                raise RuntimeError("Stage collection still exists after verified removal.")
            result["staging_collection_dropped_after_verification"] = True
            result["staging_backup_path"] = str(stage_backup)
            result["staging_ids_verified_in_operational"] = len(main_ids)

        report_path = output_dir / f"{batch_id}_migration_summary.json"
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        result["summary_path"] = str(report_path)
        return result
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--finish-profiles", action="store_true", help="Finish bounded exact-profile checks through FlashProxy")
    parser.add_argument("--apply", action="store_true", help="Insert/upsert into URLS.propiedades_captacion")
    parser.add_argument("--confirm-target", default="")
    parser.add_argument("--drop-staging-after-verify", action="store_true")
    parser.add_argument("--confirm-drop-stage", default="")
    parser.add_argument("--expected-new-batch-size", type=int, default=EXPECTED_NEW_BATCH_SIZE)
    parser.add_argument("--expected-stage-count", type=int, default=None)
    args = parser.parse_args()
    result = run(
        args.batch_id,
        finish_profiles=args.finish_profiles,
        apply=args.apply,
        confirm_target=args.confirm_target,
        drop_staging_after_verify=args.drop_staging_after_verify,
        confirm_drop_stage=args.confirm_drop_stage,
        expected_new_batch_size=args.expected_new_batch_size,
        expected_stage_count=args.expected_stage_count,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
