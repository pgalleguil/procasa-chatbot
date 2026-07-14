from __future__ import annotations

import argparse
import json
import sys
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parent.parent))

from classifier_rules import (
    build_deepseek_context,
    build_rule_context,
    classify_obvious_broker,
    company_shape_in_publisher_fields,
    is_removed_listing,
)
from config import AppConfig, get_config
from deepseek_classifier import build_description_for_llm, classify_with_deepseek
from discovery import discover_listing_urls
from downloader import download_html
from export_reports import export_batch_reports
from extractor import extract_listing_fields
from mongo_store import MongoStore
from proxy_manager import ProxyManager


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _listing_id_from_record(record: dict[str, Any]) -> str:
    if record.get("listing_id"):
        return str(record["listing_id"])
    match = re.search(r"/(\d{6,})(?:[/?#]|$)", str(record.get("url") or ""))
    return match.group(1) if match else ""


def _exclude_existing(records: list[dict[str, Any]], config: AppConfig, write_db: bool) -> tuple[list[dict[str, Any]], int]:
    if not write_db or not records:
        return records, 0
    store = MongoStore(config)
    prepared = [{**record, "listing_id": _listing_id_from_record(record)} for record in records]
    existing = store.existing_listing_ids([record["listing_id"] for record in prepared])
    fresh = [record for record in prepared if record["listing_id"] not in existing]
    return fresh, len(prepared) - len(fresh)


def _reports_dir(config: AppConfig) -> Path:
    config.ensure_layout()
    return config.reports_dir


def _discovery_path(config: AppConfig, batch_id: str) -> Path:
    return _reports_dir(config) / f"discovered_{batch_id}.json"


def _processed_path(config: AppConfig, batch_id: str) -> Path:
    return _reports_dir(config) / f"processed_{batch_id}.json"


def _latest_discovery_file(config: AppConfig) -> Path | None:
    candidates = sorted(config.reports_dir.glob("discovered_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Owner-hunt Yapo scraper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="Discover listing URLs")
    discover.add_argument("--start-url", action="append", default=[], help="Starting Yapo search URL")
    discover.add_argument("--max-pages", type=int, default=10, help="Maximum pages to scan")
    discover.add_argument("--max-urls", type=int, default=1000, help="Maximum URLs to collect")
    discover.add_argument("--until-end", action="store_true", help="Keep walking pages until the end")
    discover.add_argument("--batch-id", default="", help="Optional batch identifier")
    discover.add_argument("--dry-run", action="store_true", help="Keep discovery local only")
    discover.add_argument("--target-commune", action="append", default=[], help="Keep only listings whose search tile matches this commune")

    process = subparsers.add_parser("process", help="Process discovered HTML")
    process.add_argument("--batch-id", default="", help="Discovery batch to load")
    process.add_argument("--limit", type=int, default=100, help="Max records to process")
    process.add_argument("--offset", type=int, default=0, help="Start offset in the batch")
    process.add_argument("--write-db", action="store_true", help="Write results to MongoDB")
    process.add_argument("--dry-run", action="store_true", help="Do not write to MongoDB")
    process.add_argument("--no-llm", action="store_true", help="Disable DeepSeek calls")

    run_full = subparsers.add_parser("run-full", help="Discover and process in one step")
    run_full.add_argument("--start-url", action="append", default=[], help="Starting Yapo search URL")
    run_full.add_argument("--max-pages", type=int, default=10, help="Maximum pages to scan")
    run_full.add_argument("--max-urls", type=int, default=1000, help="Maximum URLs to collect")
    run_full.add_argument("--until-end", action="store_true", help="Keep walking pages until the end")
    run_full.add_argument("--batch-id", default="", help="Optional batch identifier")
    run_full.add_argument("--limit", type=int, default=100, help="Max records to process")
    run_full.add_argument("--offset", type=int, default=0, help="Start offset in the batch")
    run_full.add_argument("--write-db", action="store_true", help="Write results to MongoDB")
    run_full.add_argument("--dry-run", action="store_true", help="Do not write to MongoDB")
    run_full.add_argument("--no-llm", action="store_true", help="Disable DeepSeek calls")
    run_full.add_argument("--target-commune", action="append", default=[], help="Keep only listings whose search tile matches this commune")

    export = subparsers.add_parser("export", help="Export CSV and summary for a batch")
    export.add_argument("--batch-id", required=True, help="Batch identifier")

    return parser


def _batch_id_from_records(records: list[dict[str, Any]], fallback: str = "") -> str:
    if fallback:
        return fallback
    for record in records:
        batch_id = record.get("batch_id")
        if batch_id:
            return str(batch_id)
    return AppConfig().generate_batch_id()


def _save_discovery_batch(config: AppConfig, records: list[dict[str, Any]], batch_id: str) -> Path:
    path = _discovery_path(config, batch_id)
    _save_json(path, records)
    return path


def _finalize_local_classification(extracted: dict[str, Any], rule_context: dict[str, Any]) -> dict[str, Any]:
    removed = is_removed_listing(extracted)
    if removed:
        return removed

    obvious = classify_obvious_broker(extracted)
    if obvious:
        return obvious

    company_ctx = company_shape_in_publisher_fields(extracted)
    return {
        "state": "INCONCLUSIVE",
        "confidence": 0.25,
        "reason": "No obvious evidence; DeepSeek should decide.",
        "evidence": rule_context.get("owner_signal_evidence", []) or rule_context.get("weak_broker_evidence", []),
        "source": "rules_json",
        **company_ctx,
    }


def _process_single_record(
    record: dict[str, Any],
    config: AppConfig,
    proxy_manager: ProxyManager,
    *,
    no_llm: bool,
    write_db: bool,
    mongo_store: MongoStore | None,
) -> dict[str, Any]:
    url = record["url"]
    batch_id = str(record.get("batch_id") or config.generate_batch_id())
    download = download_html(url, config, proxy_manager, batch_id=batch_id)
    extracted = extract_listing_fields(download.html, source_url=url)
    extracted["html_validation_status"] = download.validation_status
    extracted["html_validation_reason"] = download.validation_reason
    if mongo_store is not None:
        extracted["publisher_profile_context"] = mongo_store.publisher_profile_context(extracted)

    description_bundle = build_description_for_llm(
        extracted.get("descripcion", extracted.get("description", "")),
        max_chars=config.deepseek_description_max_chars,
        head_chars=config.deepseek_description_head_chars,
        tail_chars=config.deepseek_description_tail_chars,
        snippet_radius=config.deepseek_description_snippet_radius,
    )

    rule_context = build_rule_context(extracted)
    classification: dict[str, Any]
    llm_result = None

    obvious = classify_obvious_broker(extracted)
    removed = is_removed_listing(extracted)
    if removed:
        classification = removed
    elif obvious:
        classification = obvious
    else:
        if no_llm:
            classification = _finalize_local_classification(extracted, rule_context)
        else:
            try:
                llm_result = classify_with_deepseek(extracted, rule_context, config, description_bundle=description_bundle)
            except Exception as exc:
                llm_result = None
                classification = _finalize_local_classification(extracted, rule_context)
                classification["llm_error"] = str(exc)
            else:
                if llm_result is not None and hasattr(llm_result, "state"):
                    classification = {
                        "state": llm_result.state,
                        "confidence": llm_result.confidence,
                        "reason": llm_result.reason,
                        "evidence": llm_result.evidence,
                        "source": "deepseek",
                        "deepseek_status": llm_result.status,
                        "deepseek_raw": llm_result.raw,
                        "trace": {
                            "deepseek_raw": llm_result.raw,
                            "deepseek_message_content": llm_result.message_content,
                            "deepseek_reasoning_content": llm_result.reasoning_content,
                            "deepseek_payload": llm_result.payload,
                        },
                    }
                else:
                    classification = _finalize_local_classification(extracted, rule_context)

    if classification.get("state") == "INCIERTO" and llm_result is None and not no_llm:
        fallback = _finalize_local_classification(extracted, rule_context)
        if fallback.get("state") != "INCIERTO":
            classification = fallback

    combined = {
        **record,
        **extracted,
        "html_path": str(download.html_path),
        "html_validation_status": download.validation_status,
        "html_validation_reason": download.validation_reason,
        "fetch_source": download.fetch_source,
        "rule_context": rule_context,
        "deepseek_context": build_deepseek_context(extracted),
        "classification": classification,
        "llm_description_original_len": description_bundle["original_len"],
        "llm_description_sent_len": description_bundle["sent_len"],
        "llm_description_truncated": description_bundle["truncated_for_llm"],
        "llm_description_strategy": description_bundle["strategy"],
        "processed_at": _utcnow(),
        "scrape_stage": (
            "ad_removed"
            if download.validation_status == "LISTING_REMOVED"
            else "needs_rescrape"
            if download.validation_status in {"INVALID", "BLOCKED"}
            else "processed"
        ),
    }

    from captacion_assignment_eligibility import mark_assignment_readiness
    mark_assignment_readiness(combined)

    if write_db and mongo_store is not None:
        mongo_store.upsert_listing(combined)

    return combined


def _process_batch(
    records: list[dict[str, Any]],
    config: AppConfig,
    *,
    no_llm: bool,
    write_db: bool,
) -> list[dict[str, Any]]:
    proxy_manager = ProxyManager.from_config(config)
    mongo_store = None
    if write_db and not config.mongo_uri:
        raise RuntimeError("MONGO_URI no configurado.")
    if write_db:
        mongo_store = MongoStore(config)

    results: list[dict[str, Any]] = []
    for record in records:
        try:
            results.append(
                _process_single_record(
                    record,
                    config,
                    proxy_manager,
                    no_llm=no_llm,
                    write_db=write_db,
                    mongo_store=mongo_store,
                )
            )
        except Exception as exc:
            results.append({**record, "error": str(exc), "scrape_stage": "error", "processed_at": _utcnow()})
    return results


def _cmd_discover(args: argparse.Namespace, config: AppConfig) -> int:
    batch_id = args.batch_id or config.generate_batch_id()
    records = discover_listing_urls(
        args.start_url,
        max_pages=args.max_pages,
        max_urls=args.max_urls,
        until_end=args.until_end,
        batch_id=batch_id,
        target_communes=args.target_commune,
    )
    records = [{**record, "batch_id": batch_id} for record in records]
    _save_discovery_batch(config, records, batch_id)
    print(json.dumps({"batch_id": batch_id, "count": len(records)}, ensure_ascii=False, indent=2))
    return 0


def _cmd_process(args: argparse.Namespace, config: AppConfig) -> int:
    batch_path = _discovery_path(config, args.batch_id) if args.batch_id else _latest_discovery_file(config)
    if batch_path is None or not batch_path.exists():
        raise RuntimeError("No discovery batch found. Run `discover` first or pass --batch-id.")
    records = _load_json(batch_path)
    if not isinstance(records, list):
        raise RuntimeError(f"Invalid discovery batch: {batch_path}")

    batch_id = args.batch_id or batch_path.stem.replace("discovered_", "", 1)
    slice_records = records[args.offset : args.offset + args.limit] if args.limit else records[args.offset :]
    processed = _process_batch(slice_records, config, no_llm=args.no_llm, write_db=args.write_db and not args.dry_run)
    processed_path = _processed_path(config, batch_id)
    _save_json(processed_path, processed)
    export_batch_reports(processed, batch_id, config)
    print(json.dumps({"batch_id": batch_id, "processed": len(processed), "path": str(processed_path)}, ensure_ascii=False, indent=2))
    return 0


def _cmd_run_full(args: argparse.Namespace, config: AppConfig) -> int:
    batch_id = args.batch_id or config.generate_batch_id()
    records = discover_listing_urls(
        args.start_url,
        max_pages=args.max_pages,
        max_urls=args.max_urls,
        until_end=args.until_end,
        batch_id=batch_id,
        target_communes=args.target_commune,
    )
    records = [{**record, "batch_id": batch_id} for record in records]
    _save_discovery_batch(config, records, batch_id)

    discovered_total = len(records)
    records, duplicates = _exclude_existing(records, config, args.write_db and not args.dry_run)
    slice_records = records[args.offset : args.offset + args.limit] if args.limit else records[args.offset :]
    processed = _process_batch(slice_records, config, no_llm=args.no_llm, write_db=args.write_db and not args.dry_run)
    processed_path = _processed_path(config, batch_id)
    _save_json(processed_path, processed)
    export_batch_reports(processed, batch_id, config)
    print(
        json.dumps(
            {
                "batch_id": batch_id,
                "discovered": discovered_total,
                "new_after_dedup": len(records),
                "duplicates_discarded": duplicates,
                "processed": len(processed),
                "discovery_path": str(_discovery_path(config, batch_id)),
                "processed_path": str(processed_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _cmd_export(args: argparse.Namespace, config: AppConfig) -> int:
    candidates = [
        _processed_path(config, args.batch_id),
        _discovery_path(config, args.batch_id),
    ]
    source = next((path for path in candidates if path.exists()), None)
    if source is None:
        raise RuntimeError(f"Batch not found: {args.batch_id}")
    records = _load_json(source)
    if not isinstance(records, list):
        raise RuntimeError(f"Invalid batch payload: {source}")
    paths = export_batch_reports(records, args.batch_id, config)
    print(json.dumps({key: str(value) for key, value in paths.items()}, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = get_config()

    if args.command == "discover":
        return _cmd_discover(args, config)
    if args.command == "process":
        return _cmd_process(args, config)
    if args.command == "run-full":
        return _cmd_run_full(args, config)
    if args.command == "export":
        return _cmd_export(args, config)
    raise RuntimeError(f"Unknown command: {args.command}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
