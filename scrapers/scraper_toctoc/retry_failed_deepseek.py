"""Bounded, resumable retry entry point for failed TOCTOC residual calls.

This reuses the production classification service and pipeline ledger. It does
not scrape, write property documents, or assign listings. A failed source
attempt is eligible only when its recorded classification reason is one of
the two approved parser failures and its original detail is present locally.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
SCRAPER_DIR = Path(__file__).resolve().parent
for _path in (str(ROOT), str(SCRAPER_DIR)):
    while _path in sys.path:
        sys.path.remove(_path)
sys.path.insert(0, str(ROOT))
sys.path.append(str(SCRAPER_DIR))

# classifier_rules belongs to the local scraper config namespace, while the
# orchestrator and assignment gate require the repository's shared config.
# Load the scraper module under a unique name, then restore/import core config.
_saved_config = sys.modules.get("config")
_config_spec = importlib.util.spec_from_file_location(
    "toctoc_retry_runtime_config", SCRAPER_DIR / "config.py"
)
if _config_spec is None or _config_spec.loader is None:
    raise RuntimeError("unable to load TOCTOC scraper configuration module")
_scraper_config_module = importlib.util.module_from_spec(_config_spec)
sys.modules[_config_spec.name] = _scraper_config_module
_config_spec.loader.exec_module(_scraper_config_module)
sys.modules["config"] = _scraper_config_module
_rules_spec = importlib.util.spec_from_file_location(
    "scrapers.scraper_toctoc.classifier_rules", SCRAPER_DIR / "classifier_rules.py"
)
if _rules_spec is None or _rules_spec.loader is None:
    raise RuntimeError("unable to load TOCTOC classifier rules")
_rules_module = importlib.util.module_from_spec(_rules_spec)
sys.modules[_rules_spec.name] = _rules_module
_rules_spec.loader.exec_module(_rules_module)
sys.modules.setdefault("classifier_rules", _rules_module)
if _saved_config is not None:
    sys.modules["config"] = _saved_config
else:
    sys.modules.pop("config", None)
    importlib.import_module("config")

build_rule_context = _rules_module.build_rule_context
classify_obvious_broker = _rules_module.classify_obvious_broker
classify_structural_broker = _rules_module.classify_structural_broker
classify_structural_owner = _rules_module.classify_structural_owner
is_strong_broker_rule = _rules_module.is_strong_broker_rule

from pipeline_entrypoint import get_scraper_config  # noqa: E402
from toctoc_pipeline import (  # noqa: E402
    MongoPipelineLedger,
    PipelineOptions,
    run_toctoc_pipeline,
)


RETRYABLE_SOURCE_REASONS = frozenset({
    "DEEPSEEK_INVALID_EMPTY_CONTENT",
    "DEEPSEEK_INVALID_JSON",
})


def select_retryable_items(source_items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select only prior AI failures explicitly approved for this retry."""
    selected = []
    for row in source_items:
        classification = row.get("classification") or {}
        reason = str(classification.get("final_reason") or "").upper()
        if row.get("ai_called") is True and reason in RETRYABLE_SOURCE_REASONS:
            selected.append(dict(row))
    return selected


def build_retry_records(
    failed_items: Iterable[dict[str, Any]],
    extracted_records: Iterable[dict[str, Any]],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    """Join failures to their original complete details, rejecting drift."""
    failures = list(failed_items)
    limit = max(0, int(max_items))
    if len(failures) != limit:
        raise ValueError(f"retry scope mismatch: expected exactly {limit}, found {len(failures)}")

    by_id: dict[str, dict[str, Any]] = {}
    for record in extracted_records:
        listing_id = str(record.get("listing_id") or record.get("codigo") or "").strip()
        if listing_id:
            if listing_id in by_id:
                raise ValueError(f"duplicate source detail for listing_id={listing_id}")
            by_id[listing_id] = dict(record)

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in failures:
        listing_id = str(row.get("listing_id") or "").strip()
        if not listing_id or listing_id in seen:
            raise ValueError("retry ledger contains a missing or duplicate listing_id")
        seen.add(listing_id)
        record = by_id.get(listing_id)
        if not record:
            raise ValueError(f"original detail unavailable for listing_id={listing_id}")
        title = str(record.get("title") or record.get("titulo") or "").strip()
        description = str(record.get("description") or record.get("descripcion") or "").strip()
        if not title or not description:
            raise ValueError(f"complete title/description required for listing_id={listing_id}")

        # The source report contains the prior failed classification. In
        # particular, historical rows can carry the superseded idType=1
        # candidate hint. Preserve its version stamps for fingerprinting, but
        # never feed that old decision back as a fresh deterministic result.
        previous_classification = record.get("classification")
        if isinstance(previous_classification, dict):
            for version_key in ("rules_version", "prompt_version", "classifier_version"):
                if not record.get(version_key) and previous_classification.get(version_key):
                    record[version_key] = previous_classification[version_key]
        record.pop("classification", None)

        # Rebuild exactly the existing local rules/context before any AI call.
        rule_result = (
            classify_structural_broker(record)
            or classify_structural_owner(record)
            or classify_obvious_broker(record)
        )
        record["rule_context"] = build_rule_context(record)
        record["classification_hint"] = rule_result
        record["strong_text_broker"] = is_strong_broker_rule(rule_result)
        record["pipeline_item_key"] = listing_id
        records.append(record)

    return records


def execute_retry(
    *,
    db: Any,
    config: Any,
    source_run_id: str,
    extracted_records: Iterable[dict[str, Any]],
    max_items: int = 40,
    retry_run_id: str | None = None,
) -> dict[str, Any]:
    """Retry a fixed failed subset through the official, gated pipeline."""
    if db is None:
        raise ValueError("Mongo database is required for durable retry ledger")
    source_rows = list(db["pipeline_run_items"].find(
        {"run_id": source_run_id, "ai_called": True},
        {"listing_id": 1, "classification": 1, "ai_called": 1},
    ))
    retry_items = select_retryable_items(source_rows)
    records = build_retry_records(retry_items, extracted_records, max_items=max_items)

    # One HTTP attempt per item. Never raise the pre-existing output-token
    # ceiling automatically; the retry ledger will show whether length occurs.
    config.deepseek_max_attempts = 1
    config.max_ai_calls_per_run = min(int(getattr(config, "max_ai_calls_per_run", max_items)), max_items)
    retry_run_id = retry_run_id or f"toctoc-deepseek-retry-{uuid.uuid4().hex}"
    pipeline_ledger = MongoPipelineLedger(db)
    if pipeline_ledger.get_run(retry_run_id):
        raise ValueError("retry run_id already exists; choose a new unique run_id")

    def preflight() -> dict[str, Any]:
        if not getattr(config, "deepseek_enabled", False) or not getattr(config, "deepseek_api_key", ""):
            return {"ok": False, "reason": "DEEPSEEK_NOT_CONFIGURED"}
        try:
            db.client.admin.command("ping")
        except Exception:
            return {"ok": False, "reason": "MONGO_PREFLIGHT_FAILED"}
        return {"ok": True}

    options = PipelineOptions(
        run_id=retry_run_id,
        max_new_items=max_items,
        test_mode=False,
        dry_run=True,
        allow_real_scraping=False,
        allow_mongo_writes=False,
        allow_assignments=False,
        allow_distribution=False,
        allow_real_ai=True,
        qa_sample_size=0,
        max_ai_calls_per_run=config.max_ai_calls_per_run,
        max_input_tokens_per_run=int(getattr(config, "max_input_tokens_per_run", 100_000)),
        max_output_tokens_per_run=int(getattr(config, "max_output_tokens_per_run", 20_000)),
        max_estimated_cost_per_run=float(getattr(config, "max_estimated_cost_per_run", 5.0)),
    )
    return run_toctoc_pipeline(
        records,
        options=options,
        config=config,
        db=db,
        ledger=pipeline_ledger,
        preflight_fn=preflight,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--details-json", required=True, type=Path)
    parser.add_argument("--max-items", type=int, default=40)
    parser.add_argument("--execute", action="store_true", help="perform the bounded API retry")
    args = parser.parse_args()
    records = json.loads(args.details_json.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("details-json must contain a JSON list")
    if not args.execute:
        raise SystemExit("Read-only selection validated; pass --execute to make the bounded retry")

    config = get_scraper_config()
    from pymongo import MongoClient

    client = MongoClient(config.mongo_uri, serverSelectionTimeoutMS=10_000)
    try:
        db = client[config.mongo_db]
        report = execute_retry(
            db=db,
            config=config,
            source_run_id=args.source_run_id,
            extracted_records=records,
            max_items=args.max_items,
        )
        print(json.dumps(report, ensure_ascii=False, default=str))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["RETRYABLE_SOURCE_REASONS", "select_retryable_items", "build_retry_records", "execute_retry"]
