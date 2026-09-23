"""Reusable real-run adapter for the TOCTOC pipeline.

The detail fetch, proxy rotation, extractor, classifier, Mongo writer and
post-scrape distributor remain the existing production components in
``run_toctoc.py``.  This module only turns a business scope into the existing
runner's input; it does not duplicate any scraping or classification logic.
"""
from __future__ import annotations

import json
import importlib.util
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRAPER_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRAPER_DIR) not in sys.path:
    sys.path.append(str(SCRAPER_DIR))

from toctoc_pipeline import ToctocRunConfig, scope_config_payload  # noqa: E402
from executive_config import resolve_scraping_config  # noqa: E402


def get_scraper_config() -> Any:
    """Load the scraper's config under a private module name.

    The repository also has a root ``config.py``.  Importing both modules as
    ``config`` in one interpreter would make the real runner depend on import
    order, so the reusable adapter keeps the two namespaces separate and
    executes the existing runner in its normal subprocess context.
    """

    path = SCRAPER_DIR / "config.py"
    spec = importlib.util.spec_from_file_location("toctoc_runtime_config", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load scraper config: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.get_config()


def _load_candidates(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        for key in ("records", "items", "candidates", "unique_records"):
            if isinstance(payload.get(key), list):
                return [dict(item) for item in payload[key] if isinstance(item, dict)]
    if not isinstance(payload, list):
        raise ValueError("candidate file must contain a list or a known list field")
    return [dict(item) for item in payload if isinstance(item, dict)]


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_prior_recovery_search_cache(
    reports_dir: Path, recovery_run_id_prefix: str | None
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Reuse completed searches from an earlier recovery of the same run."""
    if not recovery_run_id_prefix:
        return {}
    report_path = reports_dir / f"toctoc_failed_search_recovery_{recovery_run_id_prefix}.json"
    if not report_path.exists():
        return {}
    try:
        report = _load_json(report_path)
    except Exception:
        return {}

    records_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for artifact in reports_dir.glob(f"candidates_{recovery_run_id_prefix}-process-*.json"):
        try:
            rows = _load_candidates(artifact)
        except Exception:
            continue
        for row in rows:
            key = _discovery_query_key(
                row.get("comuna_solicitada") or row.get("comuna"),
                row.get("operation_requested"),
                row.get("property_type_requested"),
            )
            records_by_key.setdefault(key, []).append(dict(row))

    cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    for run in report.get("discovery_runs") or []:
        for query in run.get("queries") or []:
            status = str(query.get("status") or "").upper()
            if status not in {"SUCCESS", "SUCCESS_WITH_RESULTS", "SUCCESS_ZERO_RESULTS"}:
                continue
            key = _discovery_query_key(
                query.get("commune"), query.get("operation"), query.get("property_type")
            )
            rows = records_by_key.get(key, [])
            reported_count = int(query.get("discovered") or 0)
            # Do not reuse a completed query if its non-empty output was not
            # durably saved; retry it rather than silently losing listings.
            if reported_count > 0 and not rows:
                continue
            if rows:
                cache[key] = {
                    "records": rows,
                    "report": {
                        "discovery_degraded": False,
                        "discovery_status": "SUCCESS_WITH_RESULTS",
                        "reused_from_recovery_run": recovery_run_id_prefix,
                    },
                }
            else:
                cache[key] = {
                    "records": [],
                    "report": {
                        "discovery_degraded": False,
                        "discovery_status": "SUCCESS_ZERO_RESULTS",
                        "explicit_zero_results": False,
                        "legacy_completed_checkpoint": True,
                        "reused_from_recovery_run": recovery_run_id_prefix,
                    },
                }
    return cache


_DISCOVERY_COMPLETE_STOPS = {
    "LAST_PAGE_REACHED",
    "NEXT_NOT_FOUND",
    "NEXT_DISABLED",
    "COMPLETED",
    "SUCCESS_ZERO_RESULTS",
}


def _executive_slug(value: Any, fallback: str = "executive") -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-") or fallback


def _discovery_query_key(commune: Any, operation: Any, property_type: Any) -> tuple[str, str, str]:
    from comuna_utils import normalize_commune_slug

    return (
        normalize_commune_slug(str(commune or "")),
        str(operation or "").strip().lower(),
        str(property_type or "").strip().lower(),
    )


def _checkpoint_completed(checkpoint: dict[str, Any]) -> bool:
    return (
        str(checkpoint.get("stop_reason") or "").upper() in _DISCOVERY_COMPLETE_STOPS
        and not checkpoint.get("challenge_detected")
    )


def _discovery_query_passed(report: dict[str, Any], records: list[dict[str, Any]]) -> bool:
    """Only complete/healthy discovery outputs may enter property processing."""
    if report.get("discovery_degraded"):
        return False
    status = str(report.get("discovery_status") or "").upper()
    if status:
        return status in {"SUCCESS_WITH_RESULTS", "SUCCESS_ZERO_RESULTS"}
    if records:
        return True  # compatibility for pre-status successful fixtures/checkpoints
    return bool(report.get("explicit_zero_results"))


def _seed_resume_files(discovery_module: Any, previous_batch_id: str, current_batch_id: str) -> bool:
    """Copy a failed query's local progress into a fresh retry checkpoint.

    The original attempt remains immutable as evidence. The discovery module
    then resumes the copied checkpoint with a new batch id.
    """
    reports_dir = Path(discovery_module.REPORTS_DIR)
    staged_any = False
    old_progress = reports_dir / f"discovery_progress_{previous_batch_id}.json"
    new_progress = reports_dir / f"discovery_progress_{current_batch_id}.json"
    if old_progress.exists() and not new_progress.exists():
        payload = _load_json(old_progress)
        if isinstance(payload, list):
            for row in payload:
                if isinstance(row, dict):
                    row["recovered_from_batch"] = previous_batch_id
                    row["batch_id"] = current_batch_id
            new_progress.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            staged_any = True

    old_checkpoint = reports_dir / f"discovery_checkpoint_{previous_batch_id}.json"
    new_checkpoint = reports_dir / f"discovery_checkpoint_{current_batch_id}.json"
    if old_checkpoint.exists() and not new_checkpoint.exists():
        payload = _load_json(old_checkpoint)
        if isinstance(payload, dict):
            payload["recovered_from_batch"] = previous_batch_id
            payload["batch_id"] = current_batch_id
            new_checkpoint.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            staged_any = True
    return staged_any


def run_configured_toctoc_pipeline(
    scope: ToctocRunConfig,
    *,
    candidates_path: str | Path | None = None,
    config: Any | None = None,
    run_id: str | None = None,
    write_db: bool = True,
    allow_real_ai: bool = True,
    assignment_enabled: bool | None = None,
    max_proxy_mb: float = 0,
    reuse_html: bool = False,
    force_download: bool = False,
    reprocess_existing: bool = False,
) -> dict[str, Any]:
    """Run an internally discovered or supplied candidate set through the real runner.

    ``scope`` is generic and reusable for any executive.  The underlying
    runner remains the single source for detail fetch, extraction,
    classification, persistence and normal distribution.
    """

    config = config or get_scraper_config()
    run_id = run_id or config.generate_batch_id()
    if candidates_path is None:
        raise ValueError(
            "candidates_path is required here; use run_toctoc_pipeline_for_executive() "
            "to resolve config and discover automatically"
        )
    records = _load_candidates(candidates_path)
    if scope.max_items is not None:
        records = records[: max(0, int(scope.max_items))]
    if not records:
        return {
            "run_id": run_id,
            "run_status": "STOPPED_REQUIRES_REVIEW",
            "reason": "NO_CANDIDATES",
            "scope": scope_config_payload(scope),
        }

    discovery_path = Path(config.reports_dir) / f"discovered_{run_id}.json"
    discovery_path.parent.mkdir(parents=True, exist_ok=True)
    discovery_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    command = [
        sys.executable,
        str(SCRAPER_DIR / "run_toctoc.py"),
        "process",
        "--batch-id", run_id,
        "--limit", str(len(records)),
        "--offset", "0",
        "--proxy-mode", scope.proxy_mode,
        "--use-playwright",
        "--min-price-clp", str(int(scope.min_price_clp or 0)),
    ]
    if scope.max_price_clp is not None:
        command.extend(["--max-price-clp", str(int(scope.max_price_clp))])
    if write_db:
        command.append("--write-db")
    else:
        command.append("--dry-run")
    if not allow_real_ai:
        command.append("--no-llm")
    if reuse_html:
        command.append("--reuse-html")
    if force_download:
        command.append("--force-download")
    if reprocess_existing:
        command.append("--reprocess-existing")
    distribution_enabled = (
        bool(scope.assignment_enabled)
        if assignment_enabled is None
        else bool(assignment_enabled)
    )
    if not distribution_enabled:
        command.append("--disable-post-distribution")
    env = os.environ.copy()
    completed = subprocess.run(command, cwd=str(SCRAPER_DIR), env=env, text=True)
    processed_path = Path(config.reports_dir) / f"processed_{run_id}.json"
    processed = _load_json(processed_path) if processed_path.exists() else []
    return {
        "run_id": run_id,
        "run_status": "SUCCESS" if completed.returncode == 0 and processed_path.exists() else "FAILED",
        "runner_returncode": completed.returncode,
        "scope": scope_config_payload(scope),
        "candidate_count": len(records),
        "processed_count": len(processed),
        "processed_path": str(processed_path),
        "records": processed,
    }


def _discovery_module():
    """Load an existing scraper module without leaking its short config name."""
    scraper_path = str(SCRAPER_DIR)
    if scraper_path not in sys.path:
        sys.path.append(scraper_path)
    return _load_scraper_module("discovery")


def _load_scraper_module(name: str):
    """Import scraper modules against scraper config, restoring CRM imports."""
    previous_config = sys.modules.get("config")
    get_scraper_config()  # initializes the scraper module and its environment
    scraper_config_module = sys.modules.get("toctoc_runtime_config")
    if scraper_config_module is None or not hasattr(scraper_config_module, "AppConfig"):
        raise RuntimeError("TOCTOC scraper config module did not expose AppConfig")
    sys.modules["config"] = scraper_config_module
    try:
        import importlib
        return importlib.import_module(name)
    finally:
        if previous_config is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = previous_config


def discover_executive_scope(
    resolved: dict[str, Any],
    *,
    run_id: str,
    max_pages: int | None = None,
    max_urls_per_query: int | None = None,
    proxy_mode: str = "auto",
    proxy_manager_override: Any | None = None,
    query_cache: dict[tuple[str, str, str], dict[str, Any]] | None = None,
    resume_from_batch_ids: dict[tuple[str, str, str], str] | None = None,
) -> dict[str, Any]:
    """Discover a configured territory using the existing Playwright adapter.

    The function never processes listings or writes Mongo. Results are merged
    across operation/type searches by listing ID before they are returned.
    Individual query failures are recorded and do not discard successful
    communes or stop the remaining searches.
    """
    values = resolved.get("values") or {}
    communes = values.get("communes") or []
    operations = values.get("operations") or []
    property_types = values.get("property_types") or []
    if not communes or not operations or not property_types:
        return {
            "run_id": run_id,
            "status": "CONFIG_MISSING",
            "records": [],
            "queries": [],
            "errors": [],
            "unique_count": 0,
        }

    discovery = _discovery_module()
    schema = _load_scraper_module("crm_schema")
    from comuna_utils import normalize_commune_slug
    region_by_commune = {
        normalize_commune_slug(commune): normalize_commune_slug(region)
        for commune, region in schema.COMUNA_TO_REGION.items()
        if normalize_commune_slug(commune) and normalize_commune_slug(region)
    }
    try:
        ProxyManager = _load_scraper_module("proxy_manager").ProxyManager
        proxy_manager = proxy_manager_override
        if proxy_manager is None:
            proxy_manager = ProxyManager.from_env() if proxy_mode in {"proxy", "auto"} else ProxyManager()
        if proxy_mode == "proxy" and not proxy_manager.has_proxies():
            raise RuntimeError("proxy_mode=proxy but the existing proxy pool is empty")
    except Exception:
        if proxy_mode == "proxy":
            raise
        proxy_manager = None

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    query_rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for commune in communes:
        commune_slug = normalize_commune_slug(commune)
        region_slug = region_by_commune.get(commune_slug)
        for operation in operations:
            for property_type in property_types:
                cache_key = _discovery_query_key(commune, operation, property_type)
                query_id = f"{run_id}-{len(query_rows) + 1:04d}"
                if not region_slug:
                    errors.append({
                        "query_id": query_id,
                        "commune": str(commune),
                        "reason": "COMMUNE_REGION_NOT_MAPPED",
                    })
                    query_rows.append({
                        "commune": str(commune),
                        "operation": str(operation),
                        "property_type": str(property_type),
                        "status": "CONFIG_MISSING",
                        "discovered": 0,
                        "unique_added": 0,
                    })
                    continue
                try:
                    resume_staged = False
                    cached = query_cache.get(cache_key) if query_cache is not None else None
                    if cached is None:
                        reports_dir = getattr(discovery, "REPORTS_DIR", None)
                        current_checkpoint_path = (
                            Path(reports_dir) / f"discovery_checkpoint_{query_id}.json"
                            if reports_dir else None
                        )
                        current_progress_path = (
                            Path(reports_dir) / f"discovery_progress_{query_id}.json"
                            if reports_dir else None
                        )
                        checkpoint_reused = False
                        if current_checkpoint_path is not None and current_checkpoint_path.exists():
                            try:
                                current_checkpoint = _load_json(current_checkpoint_path)
                            except Exception:
                                current_checkpoint = {}
                            if _checkpoint_completed(current_checkpoint):
                                records = (
                                    _load_candidates(current_progress_path)
                                    if current_progress_path.exists()
                                    else []
                                )
                                page_reports = current_checkpoint.get("page_reports") or []
                                report = {
                                    "discovery_degraded": False,
                                    "discovery_status": (
                                        "SUCCESS_ZERO_RESULTS"
                                        if str(current_checkpoint.get("stop_reason") or "").upper() == "SUCCESS_ZERO_RESULTS"
                                        else "SUCCESS_WITH_RESULTS"
                                    ),
                                    "explicit_zero_results": str(current_checkpoint.get("stop_reason") or "").upper() == "SUCCESS_ZERO_RESULTS",
                                    "pagination_working": sum(
                                        1 for row in page_reports if row.get("completed")
                                    ) > 1,
                                    "expected_results": next((
                                        row.get("reported_results") for row in page_reports
                                        if row.get("reported_results") is not None
                                    ), None),
                                    "pages_fetched": sum(
                                        1 for row in page_reports if row.get("completed")
                                    ),
                                    "checkpoint_reused": True,
                                }
                                checkpoint_reused = True
                        if not checkpoint_reused:
                            previous_batch_id = (resume_from_batch_ids or {}).get(cache_key)
                            if previous_batch_id and (
                                current_checkpoint_path is None or not current_checkpoint_path.exists()
                            ):
                                resume_staged = _seed_resume_files(
                                    discovery, previous_batch_id, query_id
                                )
                            result = discovery.discover_listing_urls(
                                batch_id=query_id,
                                use_playwright=True,
                                max_pages=max_pages,
                                max_urls=max_urls_per_query,
                                operacion=str(operation),
                                tipo=str(property_type),
                                region=region_slug,
                                comuna=str(commune),
                                estado=None,
                                publicador=None,
                                precio_desde=None,
                                precio_hasta=None,
                                proxy_manager=proxy_manager,
                                return_report=True,
                            )
                            records = result.get("records", [])
                            report = result.get("report", {})
                        if query_cache is not None:
                            query_cache[cache_key] = {
                                "records": [dict(item) for item in records],
                                "report": dict(report),
                            }
                    else:
                        records = cached["records"]
                        report = cached["report"]
                    # Partial URLs from a failed/degraded search remain in its
                    # recovery artifacts, but never enter the processing scope.
                    # Only a query whose discovery health passed can supply items.
                    discovery_status = str(report.get("discovery_status") or "").upper()
                    if not discovery_status and not report.get("discovery_degraded"):
                        if records:
                            discovery_status = "SUCCESS_WITH_RESULTS"
                        elif report.get("explicit_zero_results"):
                            discovery_status = "SUCCESS_ZERO_RESULTS"
                    query_healthy = _discovery_query_passed(report, records)
                    records_for_scope = records if query_healthy else []
                    accepted = 0
                    for raw in records_for_scope:
                        record = dict(raw)
                        record.setdefault("comuna_solicitada", str(commune))
                        record.setdefault("operation_requested", str(operation))
                        record.setdefault("property_type_requested", str(property_type))
                        listing_id = str(record.get("listing_id") or record.get("url") or "").strip()
                        if not listing_id or listing_id in seen:
                            continue
                        seen.add(listing_id)
                        merged.append(record)
                        accepted += 1
                    query_rows.append({
                        "commune": str(commune),
                        "operation": str(operation),
                        "property_type": str(property_type),
                        "region": region_slug,
                        "status": discovery_status if query_healthy else "DEGRADED",
                        "cache_hit": cached is not None,
                        "discovered": len(records),
                        "unique_added": accepted,
                        "pagination_working": report.get("pagination_working"),
                        "expected_results": report.get("expected_results"),
                        "resume_staged": resume_staged,
                        "checkpoint_reused": bool(report.get("checkpoint_reused")),
                    })
                    if report.get("discovery_degraded"):
                        errors.append({
                            "query_id": query_id,
                            "reason": str(report.get("abort_reason") or "DISCOVERY_DEGRADED"),
                            "failure_category": str(report.get("failure_category") or "OTHER"),
                        })
                except Exception as exc:
                    errors.append({
                        "query_id": query_id,
                        "commune": str(commune),
                        "operation": str(operation),
                        "property_type": str(property_type),
                        "reason": f"{type(exc).__name__}: {exc}",
                    })
                    query_rows.append({
                        "commune": str(commune),
                        "operation": str(operation),
                        "property_type": str(property_type),
                        "status": "FAILED",
                        "discovered": 0,
                        "unique_added": 0,
                    })
    return {
        "run_id": run_id,
        "status": "COMPLETED_WITH_ANOMALIES" if errors else "SUCCESS",
        "records": merged,
        "queries": query_rows,
        "errors": errors,
        "unique_count": len(merged),
    }


def run_toctoc_pipeline_for_executive(
    executive: str | dict[str, Any],
    *,
    db: Any | None = None,
    config: Any | None = None,
    run_id: str | None = None,
    discovery_only: bool = False,
    write_db: bool = True,
    allow_real_ai: bool = True,
    assignment_enabled: bool = True,
    proxy_mode: str = "auto",
    max_pages: int | None = None,
    max_urls_per_query: int | None = None,
    _discovery_cache: dict[tuple[str, str, str], dict[str, Any]] | None = None,
    _resume_from_batch_ids: dict[tuple[str, str, str], str] | None = None,
) -> dict[str, Any]:
    """Reusable executive entry point: resolve → discover → existing processor.

    ``discovery_only`` is useful when configuration is incomplete. No property
    processing, property writes, AI calls, or assignment happens in that mode.
    A missing manual candidate file is never a blocker.
    """
    if db is None:
        from chatbot.storage import get_db
        db = get_db()
    resolved = resolve_scraping_config(db, executive)
    if not resolved.get("found"):
        return {
            "run_id": run_id or "",
            "run_status": "CONFIG_MISSING",
            "reason": "EXECUTIVE_NOT_FOUND_OR_AMBIGUOUS",
            "config": {key: value for key, value in resolved.items() if key != "profile"},
            "discovered_count": 0,
            "processed_count": 0,
            "assigned_count": 0,
        }

    run_id = run_id or f"toctoc-{uuid.uuid4().hex}"
    discovery_result = discover_executive_scope(
        resolved,
        run_id=run_id,
        max_pages=max_pages,
        max_urls_per_query=max_urls_per_query,
        proxy_mode=proxy_mode,
        query_cache=_discovery_cache,
        resume_from_batch_ids=_resume_from_batch_ids,
    )
    config_summary = {key: value for key, value in resolved.items() if key != "profile"}
    if discovery_only or not resolved.get("config_complete"):
        return {
            "run_id": run_id,
            "run_status": "DISCOVERY_ONLY_CONFIG_INCOMPLETE" if not resolved.get("config_complete") else discovery_result["status"],
            "reason": "DISCOVERY_ONLY_REQUESTED" if discovery_only else "REQUIRED_SCOPE_CONFIG_MISSING",
            "config": config_summary,
            "discovery": discovery_result,
            "discovered_count": discovery_result["unique_count"],
            "processed_count": 0,
            "assigned_count": 0,
        }

    if not discovery_result["records"]:
        return {
            "run_id": run_id,
            "run_status": discovery_result["status"],
            "reason": "NO_DISCOVERED_CANDIDATES",
            "config": config_summary,
            "discovery": discovery_result,
            "discovered_count": 0,
            "processed_count": 0,
            "assigned_count": 0,
        }

    values = resolved["values"]
    scope = ToctocRunConfig(
        executive_id=resolved.get("executive_id"),
        executive_name=resolved.get("executive"),
        communes=tuple(values["communes"]),
        operation=",".join(values["operations"]),
        property_types=tuple(values["property_types"]),
        min_price_clp=values["min_price_clp"],
        max_price_clp=values["max_price_clp"],
        assignment_enabled=bool(assignment_enabled),
        proxy_mode=proxy_mode,
    )
    candidates_path = Path(config.reports_dir if config else get_scraper_config().reports_dir) / f"candidates_{run_id}.json"
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    candidates_path.write_text(
        json.dumps(discovery_result["records"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result = run_configured_toctoc_pipeline(
        scope,
        candidates_path=candidates_path,
        config=config,
        run_id=run_id,
        write_db=write_db,
        allow_real_ai=allow_real_ai,
        assignment_enabled=assignment_enabled,
        reuse_html=True,
    )
    result.update({
        "config": config_summary,
        "discovery": discovery_result,
        "discovered_count": discovery_result["unique_count"],
        "assigned_count": sum(
            1 for record in result.get("records", [])
            if record.get("assigned_executive") or record.get("assignment_created")
        ),
    })
    return result


def run_toctoc_pipeline(
    executive_config: str | dict[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    """Official no-manual-file entry point for a configured executive run."""
    return run_toctoc_pipeline_for_executive(executive_config, **kwargs)


def run_toctoc_pipeline_for_executives(
    executives: list[str | dict[str, Any]],
    *,
    db: Any | None = None,
    config: Any | None = None,
    write_db: bool = True,
    allow_real_ai: bool = True,
    assignment_enabled: bool = True,
    proxy_mode: str = "auto",
    max_pages: int | None = None,
    max_urls_per_query: int | None = None,
    progress_callback: Any | None = None,
    run_id_prefix: str | None = None,
) -> dict[str, Any]:
    """Run the same configured pipeline per executive, then distribute once.

    Query results for overlapping communes are reused in-memory within this
    batch. Per-executive processing errors are recorded and do not prevent the
    remaining executives from running.
    """
    if db is None:
        from chatbot.storage import get_db
        db = get_db()
    config = config or get_scraper_config()
    discovery_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for index, executive in enumerate(executives, 1):
        executive_label = executive.get("nombre") if isinstance(executive, dict) else str(executive)
        run_id = None
        if run_id_prefix:
            stable_suffix = re.sub(r"[^a-z0-9]+", "-", str(executive_label).lower()).strip("-") or f"exec-{index}"
            run_id = f"{run_id_prefix}-{stable_suffix}"
        if progress_callback:
            progress_callback({"event": "EXECUTIVE_STARTED", "executive": executive_label})
        try:
            result = run_toctoc_pipeline_for_executive(
                executive,
                db=db,
                config=config,
                run_id=run_id,
                write_db=write_db,
                allow_real_ai=allow_real_ai,
                assignment_enabled=False,
                proxy_mode=proxy_mode,
                max_pages=max_pages,
                max_urls_per_query=max_urls_per_query,
                _discovery_cache=discovery_cache,
            )
            results.append(result)
            if progress_callback:
                progress_callback({
                    "event": "EXECUTIVE_FINISHED",
                    "executive": executive_label,
                    "run_status": result.get("run_status"),
                    "discovered": result.get("discovered_count", 0),
                    "processed": result.get("processed_count", 0),
                })
        except Exception as exc:
            failed = {
                "executive": executive_label,
                "run_status": "FAILED",
                "reason": f"{type(exc).__name__}: {exc}",
                "discovered_count": 0,
                "processed_count": 0,
                "assigned_count": 0,
            }
            results.append(failed)
            if progress_callback:
                progress_callback({"event": "EXECUTIVE_FAILED", **failed})

    distribution = {"status": "DISABLED"}
    if assignment_enabled and write_db and any(int(row.get("processed_count", 0)) > 0 for row in results):
        distribution_script = ROOT / "scripts" / "run_distribution_after_scrape.py"
        if not distribution_script.exists():
            distribution = {"status": "FAILED", "reason": "DISTRIBUTOR_SCRIPT_MISSING"}
        else:
            completed = subprocess.run(
                [sys.executable, str(distribution_script)],
                cwd=str(ROOT),
                text=True,
                capture_output=True,
                timeout=300,
            )
            distribution = {
                "status": "SUCCESS" if completed.returncode == 0 else "FAILED",
                "returncode": completed.returncode,
                "output_tail": ((completed.stdout or "") + "\n" + (completed.stderr or ""))[-5000:],
            }
        if distribution.get("status") == "SUCCESS":
            try:
                collection = db["propiedades_captacion"]
                all_ids = list(dict.fromkeys(
                    str(record.get("listing_id") or "").strip()
                    for run in results for record in run.get("records", [])
                    if str(record.get("listing_id") or "").strip()
                ))
                docs = collection.find(
                    {"origen": "toctoc", "listing_id": {"$in": all_ids}},
                    {
                        "listing_id": 1,
                        "assigned_executive": 1,
                        "assigned_to": 1,
                        "assigned_to_name": 1,
                        "gestion.ejecutivo_asignado": 1,
                        "assignment.executive_id": 1,
                        "assignment.executive_name": 1,
                    },
                ) if all_ids else []
                assigned_by_id: dict[str, str] = {}
                for doc in docs:
                    assigned = (
                        doc.get("assigned_executive")
                        or doc.get("assigned_to")
                        or (doc.get("gestion") or {}).get("ejecutivo_asignado")
                        or (doc.get("assignment") or {}).get("executive_id")
                        or (doc.get("assignment") or {}).get("executive_name")
                        or doc.get("assigned_to_name")
                    )
                    if assigned:
                        assigned_by_id[str(doc.get("listing_id") or "")] = str(assigned)
                for run in results:
                    config_info = run.get("config") or {}
                    targets = {
                        str(config_info.get("executive_id") or "").casefold(),
                        str(config_info.get("executive") or "").casefold(),
                    } - {""}
                    run_ids = {
                        str(record.get("listing_id") or "").strip()
                        for record in run.get("records", [])
                    }
                    run["assigned_count"] = sum(
                        1 for listing_id in run_ids
                        if assigned_by_id.get(listing_id, "").casefold() in targets
                    )
                    run["assigned_to_any_executive"] = sum(
                        1 for listing_id in run_ids if listing_id in assigned_by_id
                    )
            except Exception as exc:
                distribution["assignment_count_error"] = f"{type(exc).__name__}: {exc}"
    return {
        "executives": results,
        "distribution": distribution,
        "unique_discovery_queries": len(discovery_cache),
        "run_status": "COMPLETED_WITH_ANOMALIES" if any(row.get("run_status") not in {"SUCCESS", "DISCOVERY_ONLY_CONFIG_INCOMPLETE"} for row in results) or distribution.get("status") == "FAILED" else "SUCCESS",
    }


def recover_failed_toctoc_searches(
    executives: list[str | dict[str, Any]],
    *,
    previous_run_id_prefix: str,
    recovery_run_id_prefix: str | None = None,
    db: Any | None = None,
    config: Any | None = None,
    write_db: bool = True,
    allow_real_ai: bool = True,
    assignment_enabled: bool = True,
    proxy_mode: str = "auto",
    expected_searches: int | None = None,
    prior_recovery_run_id_prefix: str | None = None,
    prevalidated_searches: dict[tuple[str, str, str], dict[str, Any]] | None = None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """Retry only incomplete persisted searches, then process only their URLs.

    Complete searches are loaded from the previous discovered artifacts and
    inserted into the shared query cache. Failed unique queries are requested
    once for the entire executive group; partial checkpoints are copied to a
    fresh retry id and resumed through the existing browser paginator.
    Successful historical search results are never sent back to detail
    processing by this recovery operation.
    """
    if db is None:
        from chatbot.storage import get_db
        db = get_db()
    config = config or get_scraper_config()
    recovery_run_id_prefix = recovery_run_id_prefix or f"{previous_run_id_prefix}-recovery-v1"
    reports_dir = Path(config.reports_dir)
    final_report_path = reports_dir / f"toctoc_failed_search_recovery_{recovery_run_id_prefix}.json"
    if final_report_path.exists():
        try:
            previous_recovery_report = _load_json(final_report_path)
        except Exception:
            previous_recovery_report = {}
        if previous_recovery_report.get("run_status") == "SUCCESS":
            previous_recovery_report["report_path"] = str(final_report_path)
            previous_recovery_report["resumed_from_completed_report"] = True
            return previous_recovery_report
    resolved_rows = [resolve_scraping_config(db, item) for item in executives]
    missing = [row.get("executive", "") for row in resolved_rows if not row.get("found") or not row.get("config_complete")]
    if missing:
        return {
            "run_status": "CONFIG_MISSING",
            "reason": "EXECUTIVE_CONFIG_INCOMPLETE",
            "executives_missing_config": missing,
        }

    # The approved recovery must use the same policy for each profile. If a
    # profile has since changed its search policy, stop before issuing traffic.
    policy_keys = [
        (
            tuple(sorted(str(v).strip().lower() for v in row["values"]["operations"])),
            tuple(sorted(str(v).strip().lower() for v in row["values"]["property_types"])),
            row["values"]["min_price_clp"],
            row["values"]["max_price_clp"],
        )
        for row in resolved_rows
    ]
    approved_policy = (("arriendo", "venta"), ("departamento",), 0, None)
    if len(set(policy_keys)) != 1 or policy_keys[0] != approved_policy:
        return {
            "run_status": "STOPPED_REQUIRES_REVIEW",
            "reason": "EXECUTIVE_POLICIES_DIVERGED_FROM_APPROVED_RECOVERY_SCOPE",
            "policies": policy_keys,
            "expected_policy": approved_policy,
        }

    query_specs_by_old_id: dict[str, tuple[dict[str, Any], tuple[str, str, str]]] = {}
    for resolved in resolved_rows:
        exec_slug = _executive_slug(resolved.get("executive"))
        index = 0
        values = resolved["values"]
        for commune in values["communes"]:
            for operation in values["operations"]:
                for property_type in values["property_types"]:
                    index += 1
                    old_batch_id = f"{previous_run_id_prefix}-{exec_slug}-{index:04d}"
                    key = _discovery_query_key(commune, operation, property_type)
                    query_specs_by_old_id[old_batch_id] = ({
                        "executive": resolved["executive"],
                        "commune": commune,
                        "operation": operation,
                        "property_type": property_type,
                    }, key)

    checkpoint_rows: list[tuple[str, dict[str, Any], tuple[str, str, str], dict[str, Any]]] = []
    missing_checkpoints = []
    for old_batch_id, (spec, key) in query_specs_by_old_id.items():
        path = reports_dir / f"discovery_checkpoint_{old_batch_id}.json"
        if not path.exists():
            missing_checkpoints.append(old_batch_id)
            continue
        try:
            checkpoint = _load_json(path)
        except Exception:
            missing_checkpoints.append(old_batch_id)
            continue
        checkpoint_rows.append((old_batch_id, spec, key, checkpoint))
    checkpoint_keys = {row[2] for row in checkpoint_rows}
    uncovered_search_keys = sorted({key for _, key in query_specs_by_old_id.values()} - checkpoint_keys)
    if uncovered_search_keys or (expected_searches is not None and len(checkpoint_rows) != expected_searches):
        return {
            "run_status": "STOPPED_REQUIRES_REVIEW",
            "reason": "PREVIOUS_SEARCH_LEDGER_INCOMPLETE",
            "expected_searches": expected_searches,
            "checkpoints_found": len(checkpoint_rows),
            "missing_checkpoint_files": missing_checkpoints,
            "uncovered_search_configs": uncovered_search_keys,
        }

    failed_rows = [row for row in checkpoint_rows if not _checkpoint_completed(row[3])]
    completed_rows = [row for row in checkpoint_rows if _checkpoint_completed(row[3])]
    if expected_searches is not None and len(failed_rows) + len(completed_rows) != expected_searches:
        return {"run_status": "STOPPED_REQUIRES_REVIEW", "reason": "SEARCH_LEDGER_COUNT_MISMATCH"}

    # Recover complete query outputs from the existing discovery artifacts.
    records_by_key: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    for resolved in resolved_rows:
        artifact = reports_dir / f"discovered_{previous_run_id_prefix}-{_executive_slug(resolved.get('executive'))}.json"
        if not artifact.exists():
            continue
        try:
            rows = _load_candidates(artifact)
        except Exception:
            continue
        for record in rows:
            key = _discovery_query_key(
                record.get("comuna_solicitada"),
                record.get("operation_requested"),
                record.get("property_type_requested"),
            )
            listing_id = str(record.get("listing_id") or record.get("url") or "").strip()
            if listing_id:
                records_by_key.setdefault(key, {})[listing_id] = dict(record)

    success_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    failed_retry_batch_by_key: dict[tuple[str, str, str], str] = {}
    for old_batch_id, spec, key, checkpoint in checkpoint_rows:
        checkpoint_has_explicit_zero = (
            str(checkpoint.get("stop_reason") or "").upper() == "SUCCESS_ZERO_RESULTS"
            or any(bool(page.get("explicit_zero_results")) for page in checkpoint.get("page_reports") or [])
        )
        checkpoint_has_records = bool(records_by_key.get(key))
        if _checkpoint_completed(checkpoint) and (checkpoint_has_records or checkpoint_has_explicit_zero):
            reports = checkpoint.get("page_reports") or []
            page_count = sum(1 for row in reports if row.get("completed"))
            expected_results = next((row.get("reported_results") for row in reports if row.get("reported_results") is not None), None)
            success_by_key[key] = {
                "records": list(records_by_key.get(key, {}).values()),
                "report": {
                    "discovery_degraded": False,
                    "discovery_status": "SUCCESS_ZERO_RESULTS" if checkpoint_has_explicit_zero else "SUCCESS_WITH_RESULTS",
                    "explicit_zero_results": checkpoint_has_explicit_zero,
                    "pagination_working": page_count > 1,
                    "expected_results": expected_results,
                    "reused_from_batch": old_batch_id,
                },
            }
        else:
            current = failed_retry_batch_by_key.get(key)
            if current is None:
                failed_retry_batch_by_key[key] = old_batch_id
            else:
                current_checkpoint = next((row[3] for row in checkpoint_rows if row[0] == current), {})
                if int(checkpoint.get("last_completed_page") or 0) > int(current_checkpoint.get("last_completed_page") or 0):
                    failed_retry_batch_by_key[key] = old_batch_id

    prior_recovery_cache = _load_prior_recovery_search_cache(
        reports_dir, prior_recovery_run_id_prefix
    )
    for key, cached_result in prior_recovery_cache.items():
        success_by_key.setdefault(key, cached_result)

    # A completed duplicate query satisfies the shared config; don't request
    # it again merely because another executive's checkpoint was degraded.
    retry_batch_by_key = {
        key: old_id for key, old_id in failed_retry_batch_by_key.items()
        if key not in success_by_key
    }
    shared_cache = dict(success_by_key)
    for raw_key, cached_result in (prevalidated_searches or {}).items():
        key = _discovery_query_key(*raw_key)
        if not isinstance(cached_result, dict):
            continue
        cached_report = dict(cached_result.get("report") or {})
        cached_status = str(cached_report.get("discovery_status") or "").upper()
        if (
            not cached_report.get("discovery_degraded")
            and cached_status in {"SUCCESS_WITH_RESULTS", "SUCCESS_ZERO_RESULTS"}
        ):
            shared_cache[key] = {
                "records": [dict(row) for row in cached_result.get("records", []) if isinstance(row, dict)],
                "report": cached_report,
            }
    proxy_manager_override = None
    if proxy_mode in {"proxy", "auto"}:
        try:
            proxy_manager_class = _load_scraper_module("proxy_manager").ProxyManager
            proxy_manager_override = proxy_manager_class.from_env()
            if proxy_mode == "proxy" and not proxy_manager_override.has_proxies():
                return {
                    "run_status": "STOPPED_REQUIRES_REVIEW",
                    "reason": "PROXY_POOL_EMPTY",
                    "searches_retried": len(retry_batch_by_key),
                }
        except Exception as exc:
            if proxy_mode == "proxy":
                return {
                    "run_status": "STOPPED_REQUIRES_REVIEW",
                    "reason": f"PROXY_MANAGER_UNAVAILABLE:{type(exc).__name__}",
                    "searches_retried": len(retry_batch_by_key),
                }
    proxy_pool_size = (
        proxy_manager_override.pool_size()
        if proxy_manager_override is not None and hasattr(proxy_manager_override, "pool_size")
        else 0
    )
    discovery_runs = []
    for index, resolved in enumerate(resolved_rows, 1):
        run_id = f"{recovery_run_id_prefix}-discovery-{_executive_slug(resolved.get('executive'), f'exec-{index}') }"
        if progress_callback:
            progress_callback({"event": "RETRY_DISCOVERY_STARTED", "executive": resolved.get("executive")})
        try:
            result = discover_executive_scope(
                resolved,
                run_id=run_id,
                proxy_mode=proxy_mode,
                proxy_manager_override=proxy_manager_override,
                query_cache=shared_cache,
                resume_from_batch_ids=retry_batch_by_key,
            )
            discovery_runs.append({"executive": resolved.get("executive"), **result})
            if progress_callback:
                progress_callback({
                    "event": "RETRY_DISCOVERY_FINISHED",
                    "executive": resolved.get("executive"),
                    "status": result.get("status"),
                    "discovered": result.get("unique_count", 0),
                })
        except Exception as exc:
            discovery_runs.append({
                "executive": resolved.get("executive"),
                "run_id": run_id,
                "status": "FAILED",
                "errors": [{"reason": f"{type(exc).__name__}: {exc}"}],
                "records": [],
                "unique_count": 0,
            })

    retry_keys = set(retry_batch_by_key)
    def _cached_search_passed(key: tuple[str, str, str]) -> bool:
        cached_result = shared_cache.get(key) or {}
        cached_report = cached_result.get("report") or {}
        status = str(cached_report.get("discovery_status") or "").upper()
        if cached_report.get("discovery_degraded"):
            return False
        if status:
            return status in {"SUCCESS_WITH_RESULTS", "SUCCESS_ZERO_RESULTS"}
        if cached_result.get("records"):
            return True
        return bool(cached_report.get("explicit_zero_results"))

    recovered_keys = {key for key in retry_keys if _cached_search_passed(key)}
    still_failed_keys = retry_keys - recovered_keys
    if still_failed_keys:
        # A partial recovery is useful evidence but is not safe input for
        # classification/distribution. Keep its checkpoints and stop before
        # any detail fetch, AI call, Mongo write, or assignment.
        final_report = {
            "run_id": recovery_run_id_prefix,
            "previous_run_id_prefix": previous_run_id_prefix,
            "run_status": "COMPLETED_WITH_ANOMALIES",
            "searches_total": len(checkpoint_rows),
            "searches_previously_complete": len(completed_rows),
            "searches_previously_failed": len(failed_rows),
            "searches_retried": len(retry_batch_by_key),
            "previous_recovery_searches_reused": len(prior_recovery_cache),
            "unique_search_configs": len({row[2] for row in checkpoint_rows}),
            "recovered_searches": len(recovered_keys),
            "still_failed": len(still_failed_keys),
            "failed_search_keys": [list(key) for key in sorted(still_failed_keys)],
            "rediscovered_unique_listings": 0,
            "detail_scope_new": 0,
            "processing_skipped_due_to_discovery_failures": True,
            "property_writes": 0,
            "new_assignments": 0,
            "deepseek_calls": 0,
            "discovery_runs": [
                {k: v for k, v in row.items() if k != "records"}
                for row in discovery_runs
            ],
            "distribution": {"status": "SKIPPED", "reason": "DISCOVERY_FAILURES_REMAIN"},
            "failed_query_categories": [
                {
                    "executive": row.get("executive"),
                    "errors": row.get("errors", []),
                }
                for row in discovery_runs if row.get("errors")
            ],
        }
        report_path = reports_dir / f"toctoc_failed_search_recovery_{recovery_run_id_prefix}.json"
        report_path.write_text(json.dumps(final_report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        final_report["report_path"] = str(report_path)
        return final_report

    recovered_records: list[dict[str, Any]] = []
    seen_listing_ids: set[str] = set()
    for run in discovery_runs:
        for record in run.get("records", []):
            key = _discovery_query_key(
                record.get("comuna_solicitada"),
                record.get("operation_requested"),
                record.get("property_type_requested"),
            )
            listing_id = str(record.get("listing_id") or record.get("url") or "").strip()
            if key not in retry_keys or not listing_id or listing_id in seen_listing_ids:
                continue
            seen_listing_ids.add(listing_id)
            record = dict(record)
            record["recovery_run_id"] = recovery_run_id_prefix
            recovered_records.append(record)

    # Reuse the exact previous run's per-item outcomes. A URL already present
    # in its processed ledger must not trigger another detail fetch or another
    # AI decision just because a failed search later rediscovered the card.
    previously_processed_ids: set[str] = set()
    previously_discovered_ids: set[str] = set()
    previous_processed_rows = 0
    previous_discovered_rows = 0
    prior_recovery_processed_rows = 0
    for resolved in resolved_rows:
        artifact = reports_dir / f"discovered_{previous_run_id_prefix}-{_executive_slug(resolved.get('executive'))}.json"
        if not artifact.exists():
            continue
        try:
            rows = _load_candidates(artifact)
        except Exception:
            continue
        previous_discovered_rows += len(rows)
        previously_discovered_ids.update(
            str(row.get("listing_id") or "").strip()
            for row in rows
            if str(row.get("listing_id") or "").strip()
        )
    for pattern in (f"processed_{previous_run_id_prefix}-*.json",):
        for artifact in reports_dir.glob(pattern):
            try:
                rows = _load_candidates(artifact)
            except Exception:
                continue
            previous_processed_rows += len(rows)
            previously_processed_ids.update(
                str(row.get("listing_id") or "").strip()
                for row in rows
                if str(row.get("listing_id") or "").strip()
            )
    if prior_recovery_run_id_prefix:
        for artifact in reports_dir.glob(
            f"processed_{prior_recovery_run_id_prefix}-process-*.json"
        ):
            try:
                rows = _load_candidates(artifact)
            except Exception:
                continue
            prior_recovery_processed_rows += len(rows)
            previously_processed_ids.update(
                str(row.get("listing_id") or "").strip()
                for row in rows
                if str(row.get("listing_id") or "").strip()
            )
    recovered_records_before_ledger_filter = len(recovered_records)
    recovered_discovered_ids = {
        str(row.get("listing_id") or "").strip()
        for row in recovered_records
        if str(row.get("listing_id") or "").strip()
    }
    previous_processed_reused = len(recovered_discovered_ids & previously_processed_ids)
    previous_discovered_reused = len(
        (recovered_discovered_ids & previously_discovered_ids) - previously_processed_ids
    )
    distribution_candidate_ids = sorted(
        recovered_discovered_ids - previously_processed_ids - previously_discovered_ids
    )
    recovered_records = [
        row for row in recovered_records
        if str(row.get("listing_id") or "").strip() not in previously_processed_ids
        and str(row.get("listing_id") or "").strip() not in previously_discovered_ids
    ]
    # Keep newly recovered candidates in the distribution set across resume,
    # even if their current recovery processing ledger is already complete.
    recovery_processed_ids: set[str] = set()
    for artifact in reports_dir.glob(f"processed_{recovery_run_id_prefix}-process-*.json"):
        try:
            rows = _load_candidates(artifact)
        except Exception:
            continue
        recovery_processed_ids.update(
            str(row.get("listing_id") or "").strip()
            for row in rows
            if str(row.get("listing_id") or "").strip()
        )
    recovered_records = [
        row for row in recovered_records
        if str(row.get("listing_id") or "").strip() not in recovery_processed_ids
    ]

    processing_runs = []
    policy_groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    resolved_by_commune = {}
    for resolved in resolved_rows:
        for commune in resolved["values"]["communes"]:
            resolved_by_commune.setdefault(_discovery_query_key(commune, "", "")[0], resolved)
    for record in recovered_records:
        commune = record.get("comuna_solicitada") or record.get("comuna") or ""
        resolved = resolved_by_commune.get(_discovery_query_key(commune, "", "")[0])
        if not resolved:
            continue
        values = resolved["values"]
        key = (
            tuple(str(v).lower() for v in values["operations"]),
            tuple(str(v).lower() for v in values["property_types"]),
            values["min_price_clp"],
            values["max_price_clp"],
        )
        group = policy_groups.setdefault(key, {"resolved": resolved, "records": []})
        group["records"].append(record)

    for group_index, group in enumerate(policy_groups.values(), 1):
        resolved = group["resolved"]
        values = resolved["values"]
        records = group["records"]
        if not records:
            continue
        scope = ToctocRunConfig(
            executive_id=None,
            executive_name="multi-executive-recovery",
            communes=tuple(dict.fromkeys(str(r.get("comuna_solicitada") or r.get("comuna") or "") for r in records)),
            operation=",".join(values["operations"]),
            property_types=tuple(values["property_types"]),
            min_price_clp=values["min_price_clp"],
            max_price_clp=values["max_price_clp"],
            assignment_enabled=False,
            proxy_mode=proxy_mode,
        )
        process_run_id = f"{recovery_run_id_prefix}-process-{group_index}"
        candidates_path = reports_dir / f"candidates_{process_run_id}.json"
        candidates_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            processing_runs.append(run_configured_toctoc_pipeline(
                scope,
                candidates_path=candidates_path,
                config=config,
                run_id=process_run_id,
                write_db=write_db,
                allow_real_ai=allow_real_ai,
                assignment_enabled=False,
                reuse_html=True,
            ))
        except Exception as exc:
            processing_runs.append({
                "run_id": process_run_id,
                "run_status": "FAILED",
                "reason": f"{type(exc).__name__}: {exc}",
                "candidate_count": len(records),
                "processed_count": 0,
                "records": [],
            })

    distribution: dict[str, Any] = {"status": "DISABLED"}
    if assignment_enabled and write_db and distribution_candidate_ids:
        listing_ids = list(distribution_candidate_ids)
        executive_ids = list(dict.fromkeys(
            str(row.get("executive_id") or "").strip()
            for row in resolved_rows
            if str(row.get("executive_id") or "").strip()
        ))
        if not listing_ids or not executive_ids:
            distribution = {
                "status": "SKIPPED",
                "reason": "NO_NEW_RECOVERY_CANDIDATES_OR_EXECUTIVE_IDS",
                "candidate_listing_ids": len(listing_ids),
                "target_executives": len(executive_ids),
            }
        else:
            try:
                from api_captacion import distribute_sourced_leads

                assigned_count = distribute_sourced_leads(
                    trigger_source="toctoc_failed_search_recovery",
                    candidate_listing_ids=listing_ids,
                    candidate_portal="toctoc",
                    target_executive_ids=executive_ids,
                )
                distribution = {
                    "status": "SUCCESS",
                    "assigned": int(assigned_count or 0),
                    "candidate_listing_ids": len(listing_ids),
                    "target_executive_ids": executive_ids,
                }
            except Exception as exc:
                distribution = {
                    "status": "FAILED",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "candidate_listing_ids": len(listing_ids),
                    "target_executive_ids": executive_ids,
                }
        if distribution.get("status") == "SUCCESS":
            try:
                collection = db["propiedades_captacion"]
                docs = collection.find(
                    {"origen": "toctoc", "listing_id": {"$in": listing_ids}},
                    {
                        "listing_id": 1,
                        "assigned_executive": 1,
                        "assigned_to": 1,
                        "assigned_to_name": 1,
                        "gestion.ejecutivo_asignado": 1,
                        "assignment.executive_id": 1,
                        "assignment.executive_name": 1,
                    },
                ) if listing_ids else []
                assigned_by_executive: dict[str, int] = {}
                for doc in docs:
                    assigned = (
                        doc.get("assigned_executive")
                        or doc.get("assigned_to")
                        or (doc.get("gestion") or {}).get("ejecutivo_asignado")
                        or (doc.get("assignment") or {}).get("executive_id")
                        or (doc.get("assignment") or {}).get("executive_name")
                        or doc.get("assigned_to_name")
                    )
                    if assigned:
                        label = str(assigned)
                        assigned_by_executive[label] = assigned_by_executive.get(label, 0) + 1
                distribution["assigned_by_executive"] = assigned_by_executive
                distribution["assigned_to_hernan"] = sum(
                    count for name, count in assigned_by_executive.items()
                    if _executive_slug(name) == "hernan"
                )
            except Exception as exc:
                distribution["assignment_count_error"] = f"{type(exc).__name__}: {exc}"

    ai_calls = 0
    ai_input_tokens = 0
    ai_output_tokens = 0
    for run in processing_runs:
        for row in run.get("records", []):
            classification = row.get("classification") or {}
            if not isinstance(classification, dict):
                continue
            raw = classification.get("deepseek_raw") or (
                classification.get("trace") or {}
            ).get("deepseek_raw") or {}
            source = str(classification.get("source") or "").casefold()
            if raw or source == "deepseek":
                ai_calls += 1
            usage = raw.get("usage") if isinstance(raw, dict) else {}
            usage = usage if isinstance(usage, dict) else {}
            ai_input_tokens += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            ai_output_tokens += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

    final_report = {
        "run_id": recovery_run_id_prefix,
        "previous_run_id_prefix": previous_run_id_prefix,
        "run_status": "COMPLETED_WITH_ANOMALIES" if any(row.get("status") not in {"SUCCESS", "DISABLED"} for row in discovery_runs) or any(row.get("run_status") != "SUCCESS" for row in processing_runs) or distribution.get("status") == "FAILED" else "SUCCESS",
        "searches_total": len(checkpoint_rows),
        "searches_previously_complete": len(completed_rows),
        "searches_previously_failed": len(failed_rows),
        "searches_retried": len(retry_batch_by_key),
        "previous_recovery_searches_reused": len(prior_recovery_cache),
        "searches_reused_between_executives": max(0, len(query_specs_by_old_id) - len({key for _, _, key, _ in checkpoint_rows})),
        "unique_search_configs": len({key for _, _, key, _ in checkpoint_rows}),
        "proxy_pool_size": proxy_pool_size,
        "recovered_searches": sum(
            1 for key in retry_keys
            if shared_cache.get(key, {}).get("report", {}).get("discovery_degraded") is False
        ),
        "still_failed": sum(
            1 for key in retry_keys
            if shared_cache.get(key, {}).get("report", {}).get("discovery_degraded")
        ),
        "new_unique_listings": len(recovered_records),
        "rediscovered_unique_listings": recovered_records_before_ledger_filter,
        "previously_processed_reused": previous_processed_reused,
        "previously_discovered_rows": previous_discovered_rows,
        "previously_discovered_reused": previous_discovered_reused,
        "recovery_processed_reused_for_distribution": len(recovery_processed_ids & set(distribution_candidate_ids)),
        "previous_processed_ledger_rows": previous_processed_rows,
        "prior_recovery_processed_ledger_rows": prior_recovery_processed_rows,
        "detail_scope_new": sum(int(run.get("candidate_count", 0)) for run in processing_runs),
        "processing_runs": processing_runs,
        "discovery_runs": [
            {k: v for k, v in row.items() if k not in {"records"}}
            for row in discovery_runs
        ],
        "distribution": distribution,
        "deepseek_calls": ai_calls,
        "deepseek_input_tokens": ai_input_tokens,
        "deepseek_output_tokens": ai_output_tokens,
        "deepseek_total_tokens": ai_input_tokens + ai_output_tokens,
        "recovered_listing_ids": [str(r.get("listing_id") or "") for r in recovered_records],
    }
    report_path = reports_dir / f"toctoc_failed_search_recovery_{recovery_run_id_prefix}.json"
    report_path.write_text(json.dumps(final_report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    final_report["report_path"] = str(report_path)
    return final_report


__all__ = [
    "run_configured_toctoc_pipeline",
    "run_toctoc_pipeline_for_executive",
    "run_toctoc_pipeline",
    "run_toctoc_pipeline_for_executives",
    "recover_failed_toctoc_searches",
    "discover_executive_scope",
    "resolve_scraping_config",
]
