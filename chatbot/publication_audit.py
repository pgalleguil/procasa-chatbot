"""Local, resumable and read-only audit of the active PROCASA SUCRE portfolio."""
from __future__ import annotations

import csv
import json
import os
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chatbot.publication_data import merge_publication_snapshot
from chatbot.publication_sync import (
    OFFICE_ID,
    PROVIDERS,
    _active_mongo_property,
    _active_docs_for_listing,
    _build_incoming,
    _difference,
    _ensure_provider_rows,
    _existing_claims,
    _incoming_conflicts,
    _is_currently_active_listing,
    _office_consistency,
    _validate_listing,
    safe_fetch_html,
)
from config import Config
from scraping_convecta.scraping_prop360_ficha_completa import (
    COLLECTION_NAME,
    Prop360Client,
    get_mongo_collection,
)

PROPERTY_BUDGET_SECONDS = 35
PROP360_REQUEST_TIMEOUT_SECONDS = 8
AGGREGATOR_REQUEST_TIMEOUT_SECONDS = 8
AUDIT_PROVIDERS = ("portal_inmobiliario", "mercadolibre", "proppit", *PROVIDERS)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temp, path)


def _append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _mongo_projection_snapshot(coll) -> tuple[dict[str, dict], list[dict]]:
    docs = list(coll.find({}, {
        "codigo": 1, "oficina_id": 1, "oficina_nombre": 1,
        "estado.oficina": 1, "estado.estado_prop360": 1,
        "resumen.oficina": 1, "disponible_prop360": 1,
        "publicaciones": 1,
    }))
    by_code: dict[str, dict] = {}
    for doc in docs:
        code = str(doc.get("codigo") or "").strip()
        if code:
            by_code.setdefault(code, doc)
    return by_code, docs


def _initial_manifest(rows: list[dict], mongo_by_code: dict[str, dict], mongo_docs: list[dict], run_id: str) -> dict:
    active_rows = [row for row in rows if _is_currently_active_listing(row)]
    listing_by_code = {str(row.get("codigo") or "").strip(): row for row in active_rows
                       if row.get("codigo") not in (None, "")}
    listing_counts = Counter(str(row.get("codigo") or "").strip() for row in active_rows
                             if row.get("codigo") not in (None, ""))
    active_mongo = {code: doc for code, doc in mongo_by_code.items()
                    if _office_consistency(doc)[0] and _active_mongo_property(doc)}
    matching_docs, scope_errors = _active_docs_for_listing(_MemoryCollection(mongo_docs), active_rows)
    baseline = {}
    for code, doc in matching_docs.items():
        baseline[code] = {
            "codigo": code,
            "oficina_id": doc.get("oficina_id"),
            "oficina_nombre": doc.get("oficina_nombre"),
            "estado_oficina": (doc.get("estado") or {}).get("oficina") if isinstance(doc.get("estado"), dict) else None,
            "estado_prop360": (doc.get("estado") or {}).get("estado_prop360") if isinstance(doc.get("estado"), dict) else None,
            "disponible_prop360": doc.get("disponible_prop360"),
            "publicaciones": doc.get("publicaciones") or {},
        }
    codes = sorted(listing_by_code)
    mongo_active_codes = set(active_mongo)
    scope_by_code = {str(item.get("codigo") or ""): item for item in scope_errors}
    return {
        "run_id": run_id,
        "office_id": OFFICE_ID,
        "started_at": _utc_now(),
        "initial_listing_meta": {},
        "initial_total_active_rows": len(active_rows),
        "duplicate_prop360_codes": sorted(code for code, count in listing_counts.items() if count > 1),
        "initial_codes": codes,
        "initial_listing_by_code": listing_by_code,
        "mongo_active_count": len(mongo_active_codes),
        "mongo_only": sorted(mongo_active_codes - set(codes)),
        "prop360_only": sorted(set(codes) - set(baseline)),
        "scope_errors": scope_by_code,
        "mongo_publications_by_code": baseline,
    }


class _MemoryCollection:
    """Minimal collection adapter used only to apply the existing scope rules."""
    def __init__(self, docs: list[dict]):
        self.docs = docs

    def find(self, query, projection=None):
        codes = query.get("codigo", {}).get("$in", [])
        selected = [doc for doc in self.docs if doc.get("codigo") in codes]
        return selected


def _provider_rows(code: str, old_publications: dict, incoming: dict, proposed: dict,
                   source_meta: dict, conflicts: list[dict], scraped_at: str) -> list[dict]:
    base_rows = _difference(old_publications, proposed, code, scraped_at,
                            "Prop360.get_portales+authorized-source", "OBSERVED")
    rows = _ensure_provider_rows(base_rows, code, scraped_at,
                                 "Prop360.get_portales+authorized-source", "OBSERVED",
                                 source_meta.get("aggregator_results", []), conflicts)
    aliases = [item for item in incoming.get("aliases", []) if isinstance(item, dict)]
    aggregator_results = source_meta.get("aggregator_results", [])
    for row in rows:
        provider = row.get("portal")
        operation = row.get("operacion")
        relevant_aggregators = [item for item in aggregator_results
                                if not operation or item.get("operacion") == operation]
        matching_aliases = [item for item in aliases if item.get("portal") == provider
                            and (not operation or item.get("operacion") == operation)]
        if conflicts:
            status = "conflict"
        elif provider == "proppit":
            source_statuses = {str(item.get("source_status") or "") for item in relevant_aggregators}
            if any("timeout" in value for value in source_statuses):
                status = "timeout"
            elif any(value in {"robots_blocked", "blocked", "http_error", "redirect_limit"}
                     for value in source_statuses):
                status = "blocked"
            elif "source_verified" in source_statuses:
                status = "changed" if row.get("change_type") in {"new", "changed"} else "unchanged"
            else:
                status = "unverified"
        else:
            aggregator_statuses = {str(item.get("source_status") or "") for item in relevant_aggregators}
            if provider in PROVIDERS and any("timeout" in value for value in aggregator_statuses):
                status = "timeout"
            elif provider in PROVIDERS and any(value.startswith(("robots_", "blocked", "http_error", "redirect"))
                                                for value in aggregator_statuses):
                status = "blocked"
            elif any(item.get("verification_status") == "source_verified" for item in matching_aliases):
                status = "changed" if row.get("change_type") in {"new", "changed"} else "unchanged"
            elif row.get("change_type") == "conflict":
                status = "conflict"
            else:
                status = "unverified"
        row["status"] = status
        row["property_status"] = status
        row["evidence_level"] = (matching_aliases[0].get("verification_status") if matching_aliases else
                                 next((item.get("source_status") for item in relevant_aggregators), "not_observed"))
        row["safe_to_migrate"] = (status in {"changed", "unchanged"}
                                  and row["evidence_level"] == "source_verified")
        row["error"] = ";".join(str(item.get("reason") or item.get("source_status"))
                               for item in relevant_aggregators
                               if item.get("source_status") not in {"source_verified", "source_verified_no_destinations"}) or None
        row["conflicts"] = conflicts
    return rows


def _property_audit(client, code: str, baseline_doc: dict | None, claims: dict,
                    *, property_budget_seconds: float, fetcher=safe_fetch_html) -> dict:
    start = time.monotonic()
    scraped_at = _utc_now()
    old_publications = (baseline_doc or {}).get("publicaciones") or {}
    try:
        if isinstance(client, Prop360Client):
            remaining = max(0.25, property_budget_seconds - (time.monotonic() - start))
            payload = client.get_portales(code, budget_seconds=remaining)
        else:
            payload = client.get_portales(code)
        if not isinstance(payload, dict) or not isinstance(payload.get("publications"), list):
            raise ValueError("portal_payload_missing_or_invalid")

        def bounded_fetch(url: str) -> dict:
            remaining = property_budget_seconds - (time.monotonic() - start)
            if remaining <= 0:
                return {"status": "timeout", "reason": "property_budget_exhausted", "html": ""}
            return fetcher(url, budget_seconds=min(remaining, AGGREGATOR_REQUEST_TIMEOUT_SECONDS),
                           connect_timeout=2, read_timeout=4, retries=1)

        incoming, _raw, source_meta = _build_incoming(
            client, code, baseline_doc or {"codigo": code}, payload=payload,
            fetcher=bounded_fetch, checked_at=scraped_at,
        )
        elapsed = time.monotonic() - start
        proposed = merge_publication_snapshot(old_publications, incoming,
                                              observed_at=scraped_at,
                                              source="publication_audit_dry_run")
        conflicts = _incoming_conflicts(incoming, code, claims)
        if conflicts:
            proposed = old_publications
        rows = _provider_rows(code, old_publications, incoming, proposed,
                              source_meta, conflicts, scraped_at)
        property_timeout = elapsed > property_budget_seconds or any(row["status"] == "timeout" for row in rows)
        return {
            "codigo": code, "attempted_at": scraped_at, "elapsed_seconds": round(elapsed, 3),
            "status": "timeout" if property_timeout else "completed",
            "retryable": property_timeout, "conflicts": conflicts,
            "records": rows,
        }
    except Exception as exc:
        elapsed = time.monotonic() - start
        is_timeout = "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()
        status = "timeout" if is_timeout or elapsed >= property_budget_seconds else "unverified"
        error = {"codigo_prop360": code, "error": type(exc).__name__,
                 "reason": str(exc)[:200], "retryable": status == "timeout"}
        return {
            "codigo": code, "attempted_at": scraped_at, "elapsed_seconds": round(elapsed, 3),
            "status": status, "retryable": status == "timeout", "error": error,
            "records": [{"codigo_prop360": code, "portal": provider, "operacion": None,
                         "old_urls": [], "new_urls": [], "change_type": "not_observed",
                         "status": status, "safe_to_migrate": False,
                         "reason": "property_source_timeout" if status == "timeout" else "source_unverified",
                         "error": error["reason"]}
                        for provider in AUDIT_PROVIDERS],
        }


def _consolidate(run_dir: Path, manifest: dict, checkpoint: dict, *, final_rows: list[dict] | None = None) -> dict:
    events = checkpoint.get("latest_by_code", {})
    rows = []
    for code in manifest["initial_codes"]:
        for row in events.get(code, {}).get("records") or []:
            # Recompute from persisted evidence so resumed/final reports cannot
            # retain an optimistic flag produced before verification completed.
            row["safe_to_migrate"] = (row.get("status") in {"changed", "unchanged"}
                                      and row.get("evidence_level") == "source_verified")
            rows.append(row)
    records_path = run_dir / "results.json"
    csv_path = run_dir / "results.csv"
    _atomic_json(records_path, rows)
    fields = ("codigo_prop360", "portal", "operacion", "old_mlc", "new_mlc", "old_urls", "new_urls",
              "change_type", "status", "evidence_level", "safe_to_migrate", "reason", "error")
    temp_path = csv_path.with_suffix(".csv.tmp")
    with temp_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(row.get(key), ensure_ascii=False) if isinstance(row.get(key), (list, dict))
                             else row.get(key) for key in fields})
    os.replace(temp_path, csv_path)
    covered = set(events)
    counts: dict[str, dict[str, int]] = {}
    change_counts: dict[str, dict[str, int]] = {}
    changed_codes = set()
    safe_changed_codes = set()
    for row in rows:
        stats = counts.setdefault(str(row.get("portal") or "unknown"), {})
        status = str(row.get("status") or "unverified")
        stats[status] = stats.get(status, 0) + 1
        change_type = str(row.get("change_type") or "unchanged")
        if change_type in {"new", "changed"}:
            provider_changes = change_counts.setdefault(str(row.get("portal") or "unknown"), {})
            provider_changes[change_type] = provider_changes.get(change_type, 0) + 1
            code = str(row.get("codigo_prop360") or "")
            if code:
                changed_codes.add(code)
                if row.get("safe_to_migrate") is True:
                    safe_changed_codes.add(code)
    summary = {
        "run_id": manifest["run_id"],
        "initial_prop360_active": len(manifest["initial_codes"]),
        "initial_prop360_active_rows": manifest["initial_total_active_rows"],
        "duplicate_prop360_codes": manifest.get("duplicate_prop360_codes", []),
        "mongo_active_snapshot": manifest["mongo_active_count"],
        "effectively_auditable_codes": len(set(manifest["initial_codes"])
                                             - set(manifest.get("scope_errors", {}))
                                             - set(manifest.get("duplicate_prop360_codes", []))),
        "identity_contradictions": sorted(manifest.get("scope_errors", {})),
        "traceable_codes": len(covered),
        "untraced_codes": sorted(set(manifest["initial_codes"]) - covered),
        "retryable_codes": sorted(code for code, result in events.items() if result.get("retryable")),
        "mongo_only": manifest["mongo_only"],
        "prop360_only": manifest["prop360_only"],
        "scope_errors": manifest["scope_errors"],
        "provider_status_counts": counts,
        "provider_change_counts": change_counts,
        "codes_with_publication_changes": sorted(changed_codes),
        "codes_with_safe_verified_changes": sorted(safe_changed_codes),
        "safe_to_migrate_records": sum(1 for row in rows if row.get("safe_to_migrate") is True),
        "unsafe_or_unverified_records": sum(1 for row in rows if row.get("safe_to_migrate") is not True),
        "coverage_complete": covered == set(manifest["initial_codes"]),
        "all_provider_links_verified": bool(rows) and all(row.get("status") in {"verified", "changed", "unchanged"}
                                                          for row in rows),
        "report_json": str(records_path), "report_csv": str(csv_path),
    }
    if final_rows is not None:
        initial = manifest["initial_listing_by_code"]
        final_active = {str(row.get("codigo") or "").strip(): row for row in final_rows
                        if _is_currently_active_listing(row) and row.get("codigo") not in (None, "")}
        summary["final_prop360_active"] = len(final_active)
        summary["added_during_audit"] = sorted(set(final_active) - set(initial))
        summary["removed_during_audit"] = sorted(set(initial) - set(final_active))
        summary["listing_changed_during_audit"] = sorted(
            code for code in set(initial) & set(final_active)
            if initial[code] != final_active[code]
        )
        summary["final_listing_compared_at"] = _utc_now()
        _atomic_json(run_dir / "final_listing.json", final_active)
    _atomic_json(run_dir / "summary.json", summary)
    return summary


def run_sucre_publication_audit(*, output_dir: str | Path,
                                batch_size: int = 25,
                                resume_dir: str | Path | None = None,
                                property_budget_seconds: float = PROPERTY_BUDGET_SECONDS,
                                db=None, prop360_client=None, client_factory=None,
                                fetcher=safe_fetch_html) -> dict:
    """Run one read-only batch; ``resume_dir`` continues its saved snapshot."""
    if not 1 <= int(batch_size) <= 25:
        raise ValueError("batch_size must be between 1 and 25")
    if not 5 <= float(property_budget_seconds) <= 120:
        raise ValueError("property_budget_seconds must be between 5 and 120")
    own_client = False
    db_client = None
    owned_prop_client = prop360_client is None
    try:
        if db is None:
            db_client, coll = get_mongo_collection(COLLECTION_NAME)
            db = db_client[Config.DB_NAME]
            own_client = True
        else:
            coll = db[COLLECTION_NAME]
        if prop360_client is None:
            prop360_client = (client_factory or Prop360Client)(
                os.getenv("PROP360_EMAIL"), os.getenv("PROP360_PASSWORD"),
                delay=0, request_timeout=PROP360_REQUEST_TIMEOUT_SECONDS, max_attempts=1,
            )
        if not prop360_client.login():
            raise RuntimeError("prop360_login_failed")

        if resume_dir:
            run_dir = Path(resume_dir)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        else:
            run_id = uuid.uuid4().hex
            rows = prop360_client.fetch_listing(OFFICE_ID)
            valid, reason = _validate_listing(rows, getattr(prop360_client, "last_listing_meta", {}) or {},
                                             expected_office_id=OFFICE_ID)
            if not valid:
                raise RuntimeError("invalid_initial_listing:" + str(reason))
            mongo_by_code, mongo_docs = _mongo_projection_snapshot(coll)
            manifest = _initial_manifest(rows, mongo_by_code, mongo_docs, run_id)
            manifest["initial_listing_meta"] = getattr(prop360_client, "last_listing_meta", {}) or {}
            run_dir = Path(output_dir) / f"sucre_publication_audit_{run_id}"
            run_dir.mkdir(parents=True, exist_ok=True)
            _atomic_json(run_dir / "manifest.json", manifest)
            checkpoint = {"run_id": run_id, "latest_by_code": {}, "attempts_by_code": {},
                          "last_code_evaluated": None, "updated_at": _utc_now()}
            _atomic_json(run_dir / "checkpoint.json", checkpoint)

        current_docs = manifest.get("mongo_publications_by_code", {})
        # Conflict detection sees all current claims but every compared baseline
        # remains the frozen, minimal publication snapshot in the manifest.
        _by_code, all_claim_docs = _mongo_projection_snapshot(coll)
        claims = _existing_claims(all_claim_docs)
        latest = checkpoint.setdefault("latest_by_code", {})
        # Always finish the first pass over the immutable initial universe
        # before retrying slow properties. Otherwise one early timeout can be
        # selected again on every resume and starve all later codes.
        unattempted_codes = [code for code in manifest["initial_codes"] if code not in latest]
        retryable_codes = [code for code in manifest["initial_codes"]
                           if code in latest and latest[code].get("retryable")]
        pending_codes = unattempted_codes + retryable_codes
        batch_codes = pending_codes[:batch_size]
        for code in batch_codes:
            if code in manifest.get("scope_errors", {}):
                scope_error = manifest["scope_errors"][code]
                result = {
                    "codigo": code, "attempted_at": _utc_now(), "status": "conflict",
                    "retryable": False, "error": scope_error,
                    "records": [{"codigo_prop360": code, "portal": provider, "operacion": None,
                                 "old_urls": [], "new_urls": [], "change_type": "conflict",
                                 "status": "conflict", "safe_to_migrate": False,
                                 "reason": scope_error.get("error"), "error": scope_error.get("error")}
                                for provider in AUDIT_PROVIDERS],
                }
            else:
                result = _property_audit(
                    prop360_client, code, current_docs.get(code), claims,
                    property_budget_seconds=float(property_budget_seconds), fetcher=fetcher,
                )
            attempt = int(checkpoint.setdefault("attempts_by_code", {}).get(code, 0)) + 1
            checkpoint["attempts_by_code"][code] = attempt
            result["attempt"] = attempt
            latest[code] = result
            checkpoint["last_code_evaluated"] = code
            checkpoint["updated_at"] = _utc_now()
            _append_jsonl(run_dir / "events.jsonl", result)
            _atomic_json(run_dir / "checkpoint.json", checkpoint)
            _consolidate(run_dir, manifest, checkpoint)

        covered = set(latest) == set(manifest["initial_codes"])
        final_rows = None
        if covered:
            try:
                final_rows = prop360_client.fetch_listing(OFFICE_ID)
                valid, _reason = _validate_listing(final_rows,
                                                  getattr(prop360_client, "last_listing_meta", {}) or {},
                                                  expected_office_id=OFFICE_ID)
                if not valid:
                    final_rows = None
            except Exception:
                final_rows = None
        summary = _consolidate(run_dir, manifest, checkpoint, final_rows=final_rows)
        summary.update({"run_dir": str(run_dir), "run_id": manifest["run_id"],
                        "batch_checked": len(batch_codes), "mongo_writes": 0,
                        "last_code_evaluated": checkpoint.get("last_code_evaluated"),
                        "remaining_codes": len(manifest["initial_codes"]) - len(latest) +
                                           sum(1 for value in latest.values() if value.get("retryable")),
                        "property_budget_seconds": float(property_budget_seconds)})
        _atomic_json(run_dir / "summary.json", summary)
        return summary
    finally:
        if owned_prop_client and prop360_client is not None:
            close = getattr(getattr(prop360_client, "client", None), "close", None)
            if callable(close):
                close()
        if own_client and db_client is not None:
            db_client.close()
