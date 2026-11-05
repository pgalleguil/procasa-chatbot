"""Execute issue #122's Maipu Yapo scope and persist into the operational collection.

Every Yapo request is required to use the single configured FlashProxy endpoint.
The temporary stage is removed only after verified import; there are no DeepSeek
calls, assignments, notifications, or post-scrape distribution.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scrapers.scraper_yapo.classification_pipeline import CLASSIFIER_VERSION
from scrapers.scraper_yapo.config import get_config
from scrapers.scraper_yapo.pilot import run_search
from scrapers.scraper_yapo.proxy_manager import ProxyManager
from scrapers.scraper_yapo.search_config import SearchConfig
from scrapers.scraper_yapo.staging import (
    OPERATIONAL_COLLECTION,
    SOURCE_PORTAL,
    STAGING_COLLECTION,
    STAGING_DATABASE,
    ensure_unique_identity_index,
    upsert_staging_record,
)

UF_VALUE_CLP = 41268.88
UF_DATE = "2026-11-04"
UF_SOURCE = "Servicio de Impuestos Internos, tabla diaria UF 2026"


def _assert_atlas_flashproxy_only(config: Any) -> None:
    if config.mongo_db != STAGING_DATABASE or config.mongo_collection != OPERATIONAL_COLLECTION:
        raise RuntimeError("MongoDB environment does not match URLS.propiedades_captacion.")
    host = urlsplit(config.mongo_uri).hostname or ""
    if not host.endswith("mongodb.net"):
        raise RuntimeError("MongoDB target is not Atlas; no Yapo request or write started.")
    manager = ProxyManager.from_config(config)
    if manager.provider != "flashproxy" or len(manager._proxies) != 1:
        raise RuntimeError("A single configured FlashProxy endpoint is required; no Yapo request started.")
    proxy = urlsplit(manager._proxies[0])
    proxy_host = (proxy.hostname or "").lower().rstrip(".")
    if (proxy_host != "flashproxy.io" and not proxy_host.endswith(".flashproxy.io")):
        raise RuntimeError("Configured endpoint is not FlashProxy; no Yapo request started.")
    if not proxy.username or not proxy.password:
        raise RuntimeError("FlashProxy credentials are unavailable; no Yapo request started.")


def _classification_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    classification = record.get("classification")
    if not isinstance(classification, dict):
        classification = {"state": "INCIERTO", "reason": "No hubo evidencia suficiente para clasificar."}
    return {
        **classification,
        "classifier_version": CLASSIFIER_VERSION,
        "deepseek_call_executed": False,
        "calculated_at": datetime.now(timezone.utc).isoformat(),
    }


def _record_callback(collection: Any, batch_id: str):
    calls = 0

    def persist(record: dict[str, Any], phase: str) -> None:
        nonlocal calls
        record = dict(record)
        record.setdefault("source_portal", SOURCE_PORTAL)
        record.setdefault("source", "yapo_issue122_live_scrape")
        record["staging_batch_id"] = batch_id
        record["staging_only"] = True
        record["crm_promotion_allowed"] = False
        record["distribution_blocked"] = True
        record["assignment_ready"] = False
        classification = _classification_snapshot(record)
        upsert_staging_record(
            collection,
            record,
            v3_classification=classification,
            batch_id=batch_id,
        )
        # The first callback provides an interruption checkpoint. The final
        # callback updates only this batch's newly staged scrape fields, never
        # the canonical 482 rows or CRM/human-managed records.
        if phase == "final":
            identity = {
                "source_portal": SOURCE_PORTAL,
                "listing_id": str(record.get("listing_id") or ""),
                "staging_batch_id": batch_id,
            }
            final_fields = {
                "classification": record.get("classification") or {},
                "classification_evidence": record.get("classification_evidence") or [],
                "classification_debug": record.get("classification_debug") or {},
                "publisher_profile_context": record.get("publisher_profile_context") or {},
                "pipeline_state": record.get("pipeline_state") or "UNCERTAIN_PENDING_REVIEW",
                "pipeline_complete": bool(record.get("pipeline_complete")),
                "filter_status": record.get("filter_status") or "PENDIENTE_VALIDACION",
                "filter_validation": record.get("filter_validation") or {},
                "assignment_ready": False,
                "crm_promotion_allowed": False,
                "distribution_blocked": True,
                "scrape_stage": record.get("scrape_stage") or "processed",
                "deepseek_call_executed": False,
                "mongo_write_executed": False,
            }
            if isinstance(record.get("details"), dict):
                final_fields["details"] = record["details"]
            collection.update_one(identity, {"$set": final_fields})
        calls += 1

    return persist, lambda: calls


def run(*, apply: bool, confirm_target: str) -> dict[str, Any]:
    if not apply or confirm_target != f"{STAGING_DATABASE}.{STAGING_COLLECTION}":
        raise RuntimeError("Live execution requires --apply --confirm-target URLS.yapo_preimportacion.")
    config = get_config()
    _assert_atlas_flashproxy_only(config)
    config.uf_valor_clp = UF_VALUE_CLP
    config.uf_fecha = UF_DATE
    config.uf_source = UF_SOURCE

    from pymongo import MongoClient

    client = MongoClient(config.mongo_uri, serverSelectionTimeoutMS=20000, connectTimeoutMS=10000)
    try:
        client.admin.command("ping")
        database = client[STAGING_DATABASE]
        names = database.list_collection_names()
        if OPERATIONAL_COLLECTION not in names:
            raise RuntimeError("Operational collection missing; live scrape stopped.")
        operational = database[OPERATIONAL_COLLECTION]
        staging = database[STAGING_COLLECTION]
        if STAGING_COLLECTION in names and staging.count_documents({}):
            raise RuntimeError("Temporary staging still contains an earlier batch; preserve and migrate it first.")
        index = ensure_unique_identity_index(staging, apply=True)
        if not index.get("ready"):
            raise RuntimeError("Could not create the temporary staging unique identity index.")

        existing_ids: set[str] = set()
        existing_urls: set[str] = set()
        for collection in (operational, staging):
            for doc in collection.find(
                {"$or": [{"source_portal": SOURCE_PORTAL}, {"origen": SOURCE_PORTAL}]},
                {"listing_id": 1, "url": 1, "canonical_url": 1, "_id": 0},
            ):
                if doc.get("listing_id"):
                    existing_ids.add(str(doc["listing_id"]))
                for value in (doc.get("url"), doc.get("canonical_url")):
                    if value:
                        existing_urls.add(str(value))

        batch_id = config.generate_batch_id().replace("owner_hunt", "issue122_maipu")
        output_dir = config.reports_dir / "issue122_maipu"
        output_dir.mkdir(parents=True, exist_ok=True)
        persist, callback_count = _record_callback(staging, batch_id)
        search = SearchConfig(
            region="Región Metropolitana",
            communes=("Maipú",),
            operation="venta",
            property_type="casa",
            price_min=3000,
            price_max=6000,
            price_currency="UF",
            max_pages=None,
            max_listings=None,
            output_dir=str(output_dir),
            batch_id=batch_id,
            live=True,
        )
        summary = run_search(
            search,
            config,
            additional_existing_ids=existing_ids,
            additional_existing_urls=existing_urls,
            record_callback=persist,
        )

        staged_docs = list(staging.find(
            {"source_portal": SOURCE_PORTAL, "staging_batch_id": batch_id},
            {
                "listing_id": 1, "classification": 1, "availability_status": 1,
                "availability_source": 1, "availability_checked_at": 1,
                "filter_status": 1, "filter_validation": 1,
                "contact_phone": 1, "contact_email": 1,
                "html_validation_status": 1, "description_available": 1,
                "extraction_issues": 1, "assignment_ready": 1, "_id": 0,
            },
        ))
        import_result: dict[str, Any] = {}
        stage_dropped = False
        if staged_docs:
            from scrapers.scraper_yapo.import_yapo_live_batch import run as import_live_batch

            import_result = import_live_batch(
                batch_id,
                finish_profiles=True,
                apply=True,
                confirm_target=f"{STAGING_DATABASE}.{OPERATIONAL_COLLECTION}",
                drop_staging_after_verify=True,
                confirm_drop_stage=f"{STAGING_DATABASE}.{STAGING_COLLECTION}",
                expected_new_batch_size=len(staged_docs),
                expected_stage_count=len(staged_docs),
            )
            stage_dropped = bool(import_result.get("staging_collection_dropped_after_verification"))
        else:
            if staging.count_documents({}) != 0:
                raise RuntimeError("No new batch was reported but temporary staging contains records; preserve it for recovery.")
            if STAGING_COLLECTION in database.list_collection_names():
                database.drop_collection(STAGING_COLLECTION)
            stage_dropped = STAGING_COLLECTION not in database.list_collection_names()

        class_counts = Counter(str((doc.get("classification") or {}).get("state") or "INCIERTO") for doc in staged_docs)
        active_verified = [
            doc for doc in staged_docs
            if doc.get("availability_status") == "ACTIVO"
            and str(doc.get("availability_source") or "").lower() == "flashproxy"
            and bool(doc.get("availability_checked_at"))
        ]
        owner_states = {"DUEÑO_SEGURO", "DUEÑO_PROBABLE"}
        usable = [
            doc for doc in active_verified
            if str((doc.get("classification") or {}).get("state") or "") in owner_states
            and doc.get("filter_status") == "PASA"
            and bool(doc.get("contact_phone") or doc.get("contact_email"))
            and doc.get("html_validation_status") == "OK"
            and bool(doc.get("description_available"))
            and not (doc.get("extraction_issues") or [])
        ]
        result = {
            "batch_id": batch_id,
            "scope": search.to_dict(),
            "flashproxy_only": True,
            "uf_reference": {"clp_per_uf": UF_VALUE_CLP, "date": UF_DATE, "source": UF_SOURCE},
            "live_search": summary,
            "temporary_staging": {
                "existing_identity_count_before": len(existing_ids),
                "new_records_staged": len(staged_docs),
                "staging_callbacks": callback_count(),
                "classification_counts": dict(class_counts),
                "currently_active_confirmed": len(active_verified),
                "usable_owner_candidates": int(import_result.get("usable_for_assignment_now", len(usable))),
                "usable_definition": "FlashProxy explicitly confirms ACTIVO; DUEÑO_SEGURO/DUEÑO_PROBABLE; all requested filters pass; public phone/email; valid HTML and complete description; no extraction issues.",
                "assignment_authorized": 0,
                "operational_collection_writes": int(import_result.get("new_records_written_to_operational", 0)),
                "distribution_runs": 0,
                "deepseek_calls": 0,
                "dropped_after_verification": stage_dropped,
            },
            "operational_import": import_result,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        report_path = output_dir / f"{batch_id}_execution_summary.json"
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        result["execution_summary_path"] = str(report_path)
        return result
    finally:
        client.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Run live Yapo search; import to URLS.propiedades_captacion after readback verification")
    parser.add_argument("--confirm-target", default="", help="Must equal URLS.yapo_preimportacion for temporary checkpoints")
    return parser


if __name__ == "__main__":
    args = _parser().parse_args()
    print(json.dumps(run(apply=args.apply, confirm_target=args.confirm_target), ensure_ascii=False, indent=2, default=str))
