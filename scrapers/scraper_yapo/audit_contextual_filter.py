"""Offline, privacy-preserving audit of the deterministic Yapo classifier."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import importlib.util
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .classifier_rules import classify_obvious_broker, classify_yapo_listing
    from .downloader import validate_html
    from .extractor import extract_listing_fields
except ImportError:  # direct script execution
    from scrapers.scraper_yapo.classifier_rules import classify_obvious_broker, classify_yapo_listing
    from scrapers.scraper_yapo.downloader import validate_html
    from scrapers.scraper_yapo.extractor import extract_listing_fields


ROOT = Path(__file__).resolve().parent
DEFAULT_BASELINE = ROOT / "reports" / "task2ab_offline_audit_20261102_1e74032.json"
CASE_COLUMNS = [
    "case_id_sha256_16", "html_status", "baseline_state", "first_filter_state",
    "current_state", "current_rule", "publisher_block", "commercial_identity_confirmed",
    "title_len", "description_len", "description_source", "description_truncated",
    "owner_candidate_terms", "broker_declaration_terms", "third_party_terms",
    "direct_deal_commercial_contradiction", "trato_directo_commercial_contradiction", "extraction_error",
]
AUDIT_HTML_PARSER = "lxml" if importlib.util.find_spec("lxml") else "html.parser"


def _audit_one(item: tuple[str, str]) -> tuple[list[Any], dict[str, Any]]:
    raw_path, baseline_state = item
    path = Path(raw_path)
    case_id = _case_id(path)
    try:
        html = path.read_text(encoding="utf-8")
        validation = validate_html(html)
        status = str(validation.get("status") or "UNKNOWN")
        if status in {"BLOCKED", "INVALID", "LISTING_REMOVED"}:
            row = [case_id, status, baseline_state, None, None, "technical_validation", False, False, 0, 0, "", False, [], [], [], False, False, ""]
            return row, {"status": status, "technical": True, "readable": True}
        extracted = extract_listing_fields(
            html, parser=AUDIT_HTML_PARSER, include_images=False, include_listing_details=False
        )
        publisher_block = bool(extracted.get("publisher_field_provenance") or {})
        first = classify_obvious_broker(extracted)
        first_state = str(first.get("state")) if first else "INCIERTO"
        result = classify_yapo_listing(extracted) or {}
        state = str(result.get("state") or "INCIERTO")
        contextual = result.get("contextual_analysis") or {}
        owner_terms = list(contextual.get("owner_signal_candidates") or [])
        broker_terms = list(contextual.get("broker_self_declarations") or [])
        third_party = list(contextual.get("broker_third_party_mentions") or [])
        row = [
            case_id, status, baseline_state, first_state, state, str(result.get("source") or ""),
            publisher_block, state == "CORREDOR_SEGURO",
            int(extracted.get("title_original_len", len(extracted.get("title") or ""))),
            int(extracted.get("descripcion_len", len(extracted.get("description") or ""))),
            str(extracted.get("descripcion_source") or ""), bool(extracted.get("descripcion_is_truncated")),
            owner_terms, broker_terms, third_party,
            bool(contextual.get("direct_deal_with_commercial_contradictions")),
            bool(contextual.get("trato_directo_with_commercial_contradictions")), "",
        ]
        return row, {
            "status": status, "state": state, "first_state": first_state, "readable": True,
            "publisher_block": publisher_block, "owner_terms": owner_terms,
            "direct_deal_contradiction": bool(contextual.get("direct_deal_with_commercial_contradictions")),
            "trato_directo_contradiction": bool(contextual.get("trato_directo_with_commercial_contradictions")),
            "identity_owner_conflict": bool(contextual.get("identity_and_owner_signal_conflict")),
            "baseline_state": baseline_state,
        }
    except UnicodeDecodeError:
        return [case_id, "UNREADABLE_UTF8", baseline_state, None, None, "error", False, False, 0, 0, "", False, [], [], [], False, False, "utf8_decode_error"], {"status": "UNREADABLE_UTF8", "error": "utf8_decode_error", "readable": False}
    except Exception as exc:
        code = type(exc).__name__
        return [case_id, "EXTRACTION_ERROR", baseline_state, None, None, "error", False, False, 0, 0, "", False, [], [], [], False, False, code], {"status": "EXTRACTION_ERROR", "error": code, "readable": True}


def _case_id(path: Path) -> str:
    return hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16]


def audit_html_files(
    html_files: list[Path], *, baseline_path: Path | None = None, source_commit: str = "unknown", workers: int = 1
) -> dict[str, Any]:
    baseline_by_id: dict[str, str] = {}
    if baseline_path and baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        for row in baseline.get("cases", []):
            if isinstance(row, list) and len(row) >= 18:
                baseline_by_id[str(row[0])] = str(row[17])

    statuses: Counter[str] = Counter()
    states: Counter[str] = Counter()
    transitions: Counter[str] = Counter()
    extraction_errors: Counter[str] = Counter()
    cases: list[list[Any]] = []
    totals = Counter()

    ordered = sorted(html_files, key=lambda item: item.name)
    tasks = [(str(path), baseline_by_id.get(_case_id(path), "")) for path in ordered]
    if workers > 1 and len(tasks) > 1:
        print(f"Auditando {len(tasks)} HTML locales con {workers} procesos ({AUDIT_HTML_PARSER}).", flush=True)
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = []
            for index, result in enumerate(pool.map(_audit_one, tasks, chunksize=16), start=1):
                results.append(result)
                if index % 500 == 0:
                    print(f"Procesados {index}/{len(tasks)} HTML.", flush=True)
    else:
        results = []
        for index, item in enumerate(tasks, start=1):
            results.append(_audit_one(item))
            if workers > 1 and index % 500 == 0:
                print(f"Procesados {index}/{len(tasks)} HTML.", flush=True)
    totals["html_files_examined"] = len(tasks)
    for row, metrics in results:
        cases.append(row)
        status = str(metrics.get("status") or "UNKNOWN")
        statuses[status] += 1
        totals["html_utf8_readable"] += int(bool(metrics.get("readable")))
        if status in {"BLOCKED", "INVALID", "LISTING_REMOVED"}:
            totals[{"BLOCKED": "technical_blocked", "INVALID": "technical_invalid", "LISTING_REMOVED": "technical_removed"}[status]] += 1
            continue
        if status in {"UNREADABLE_UTF8", "EXTRACTION_ERROR"}:
            totals["errors_or_parse_failures"] += 1
            extraction_errors[str(metrics.get("error") or status)] += 1
            continue
        totals["html_valid_ok"] += 1
        totals["publisher_block_identified"] += int(bool(metrics.get("publisher_block")))
        state = str(metrics.get("state") or "INCIERTO")
        states[state] += 1
        first_state = str(metrics.get("first_state") or "INCIERTO")
        if first_state != state:
            transitions[f"{first_state}->{state}"] += 1
        owner_terms = metrics.get("owner_terms") or []
        totals["with_owner_signal_candidates"] += int(bool(owner_terms))
        totals["uncertain_with_owner_signals"] += int(state == "INCIERTO" and bool(owner_terms))
        totals["direct_deal_commercial_contradictions"] += int(bool(metrics.get("direct_deal_contradiction")))
        totals["trato_directo_commercial_contradictions"] += int(bool(metrics.get("trato_directo_contradiction")))
        totals["identity_and_owner_signal_conflicts"] += int(bool(metrics.get("identity_owner_conflict")))
        totals["publisher_identity_confirmed"] += int(state == "CORREDOR_SEGURO")
        totals["cases_without_owner_or_broker_context_signal"] += int(state == "INCIERTO" and not owner_terms)
        baseline_state = str(metrics.get("baseline_state") or "")
        totals["changed_vs_previous_audit"] += int(bool(baseline_state and baseline_state != state))

    commercial_total = sum(states.values())
    summary = {
        "html_files_examined": totals["html_files_examined"],
        "html_utf8_readable": totals["html_utf8_readable"],
        "html_valid_ok": totals["html_valid_ok"],
        "technical_status_counts": {
            "BLOCKED": totals["technical_blocked"],
            "INVALID": totals["technical_invalid"],
            "LISTING_REMOVED": totals["technical_removed"],
        },
        "commercial_classifications_counted": commercial_total,
        "publisher_block_identified": totals["publisher_block_identified"],
        "commercial_identity_confirmed": states["CORREDOR_SEGURO"],
        "CORREDOR_SEGURO": states["CORREDOR_SEGURO"],
        "CORREDOR_PROBABLE": states["CORREDOR_PROBABLE"],
        "INCIERTO": states["INCIERTO"],
        "uncertain_with_owner_signals": totals["uncertain_with_owner_signals"],
        "all_cases_with_owner_signal_candidates": totals["with_owner_signal_candidates"],
        "direct_deal_with_commercial_contradictions": totals["direct_deal_commercial_contradictions"],
        "trato_directo_with_commercial_contradictions": totals["trato_directo_commercial_contradictions"],
        "identity_and_owner_signal_conflicts": totals["identity_and_owner_signal_conflicts"],
        "state_changes_first_filter_to_second_filter": dict(transitions),
        "second_filter_classification_changes": sum(transitions.values()),
        "technical_rows_with_previous_commercial_state": {
            status: dict(Counter(str(row[2] or "unavailable") for row in cases if row[1] == status))
            for status in ("BLOCKED", "INVALID", "LISTING_REMOVED")
        },
        "changed_vs_previous_audit": totals["changed_vs_previous_audit"],
        "cases_without_owner_or_broker_context_signal": totals["cases_without_owner_or_broker_context_signal"],
        "errors_or_parse_failures": totals["errors_or_parse_failures"],
        "extraction_error_counts": dict(extraction_errors),
        "human_review_cases": states["CORREDOR_PROBABLE"] + states["INCIERTO"],
    }
    return {
        "source_commit": source_commit,
        "audit_mode": "offline_local_html_only",
        "html_parser": AUDIT_HTML_PARSER,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "privacy": "No names, phone numbers, titles, descriptions, raw HTML, or URLs are included; case IDs are SHA-256-derived.",
        "limitations": ["No human-verified labels; no accuracy or false-positive rate is claimed.", "Owner signals are candidates only; no DUEÑO state is emitted."],
        "external_io": {"yapo_requests": 0, "proxy_requests": 0, "mongodb_reads": 0, "mongodb_writes": 0, "deepseek_calls": 0, "deployments": 0},
        "summary": summary,
        "html_status_counts": dict(statuses),
        "case_columns": CASE_COLUMNS,
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html-dir", type=Path, default=ROOT / "html_dumps")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--source-commit", default="unknown")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    files = list(args.html_dir.glob("*.html"))
    report = audit_html_files(files, baseline_path=args.baseline, source_commit=args.source_commit, workers=max(1, args.workers))
    output = args.output or ROOT / "reports" / f"task2b_contextual_audit_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps({"report": str(output), **report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
