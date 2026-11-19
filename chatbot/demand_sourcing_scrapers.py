"""Bounded adapters for the existing Yapo and TocToc pipelines.

Only the standalone demand worker imports this module. Both scrapers run in
dry-run mode so they cannot distribute captaciones or mutate CRM ownership;
the worker persists sanitized references in the shared demand index.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import quote_plus
from uuid import uuid4
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
_PROCESSED_YAPO = re.compile(r'"processed_path"\s*:\s*"([^"]+\.json)"')
_PROCESSED_TOCTOC = re.compile(r"Processed\s+\d+\s+records\s*->\s*(.+?\.json)")


class ScraperAdapterError(RuntimeError):
    pass


def _run(command: list[str], *, timeout: int) -> str:
    try:
        completed = subprocess.run(
            command, cwd=str(ROOT), capture_output=True, text=True,
            timeout=timeout, check=False, shell=False,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise ScraperAdapterError("scraper_timeout") from exc
    except OSError as exc:
        raise ScraperAdapterError("scraper_process_start_failed") from exc
    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    if completed.returncode != 0:
        raise ScraperAdapterError(f"scraper_exit_{completed.returncode}")
    return output


def _read_result(output: str, pattern: re.Pattern) -> list[dict[str, Any]]:
    matches = list(pattern.finditer(output))
    if not matches:
        raise ScraperAdapterError("scraper_result_path_missing")
    result_path = Path(matches[-1].group(1).strip().strip('"'))
    if not result_path.is_absolute():
        result_path = (ROOT / result_path).resolve()
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScraperAdapterError("scraper_result_unreadable") from exc
    if not isinstance(result, list):
        raise ScraperAdapterError("scraper_result_shape_invalid")
    return [row for row in result if isinstance(row, dict)]


def _cleanup_batch_artifacts(source: str, batch_id: str) -> None:
    """Remove only reports/HTML created under this generated batch identifier."""
    scraper_root = ROOT / ("scraper" if source == "yapo" else Path("scrapers") / "scraper_toctoc")
    reports = scraper_root / "reports"
    if reports.exists():
        for path in reports.glob(f"*{batch_id}*"):
            if path.is_file() and path.resolve().parent == reports.resolve():
                path.unlink(missing_ok=True)
    html_root = scraper_root / "html_dumps"
    batch_html = html_root / batch_id
    if (batch_html.exists() and batch_html.is_dir()
            and batch_html.resolve().parent == html_root.resolve()
            and batch_id.startswith("demand_")):
        shutil.rmtree(batch_html)


def _yapo(job: dict) -> dict:
    criteria = job.get("criteria_envelope") or {}
    communes = [str(value).strip() for value in criteria.get("comunas") or [] if str(value).strip()]
    if not communes:
        return {"status": "skipped", "reason": "verified_commune_required", "records": []}
    limits = job.get("cost_controls") or {}
    max_pages = max(1, min(int(limits.get("max_pages_per_source") or 2), 3))
    max_urls = max(1, min(int(limits.get("max_urls_per_source") or 30), 60))
    max_records = max(1, min(int(limits.get("max_records_per_source") or 30), max_urls))
    kinds = [str(value).upper() for value in criteria.get("property_types") or [] if str(value).strip()]
    template = os.getenv("YAPO_SEARCH_URL_TEMPLATE", "https://www.yapo.cl/search?q={query}")
    if "{query}" not in template:
        return {"status": "skipped", "reason": "configured_yapo_search_template_missing_query",
                "records": []}
    segments = [(commune, kind) for commune in communes for kind in (kinds or [""])]
    selected = segments[:min(len(segments), max_pages, max_urls)]
    segment_count = max(1, len(selected))
    pages_per_segment = max(1, max_pages // segment_count)
    urls_per_segment = max(1, max_urls // segment_count)
    records_per_segment = max(1, max_records // segment_count)
    records: list[dict[str, Any]] = []
    failures = []
    for commune, kind in selected:
        operation = "arriendo" if str(criteria.get("operation") or "VENTA").upper() == "ARRIENDO" else "venta"
        query = " ".join(value for value in (operation, kind.lower(), commune) if value)
        start_url = template.format(query=quote_plus(query))
        batch_id = "demand_" + uuid4().hex[:20]
        command = [sys.executable, str(ROOT / "scraper_yapo" / "run_owner_hunt.py"),
                   "run-full", "--start-url", start_url,
                   "--max-pages", str(pages_per_segment), "--max-urls", str(urls_per_segment),
                   "--limit", str(records_per_segment), "--batch-id", batch_id,
                   "--dry-run", "--no-llm", "--skip-reports", "--target-commune", commune]
        try:
            output = _run(command, timeout=360)
            records.extend(_read_result(output, _PROCESSED_YAPO)[:records_per_segment])
        except ScraperAdapterError as exc:
            failures.append({"commune": commune, "type": kind, "reason": str(exc)})
        finally:
            _cleanup_batch_artifacts("yapo", batch_id)
    records = records[:max_records]
    return {"status": "partial" if failures else "completed", "records": records,
            "processed": len(records), "failures": failures,
            "coverage": {"requested_segments": len(segments),
                         "searched_segments": len(selected),
                         "omitted_segments": len(segments) - len(selected)},
            "filters_applied": {"communes": communes, "types_in_search_query": kinds,
                                "operation_in_search_query": criteria.get("operation"),
                                "type": False, "max_price_clp": False},
            "cache_policy": "shared_catalog_and_job_window",
            "bounded_pages": max_pages, "bounded_urls": max_urls}


def _toctoc(job: dict) -> dict:
    criteria = job.get("criteria_envelope") or {}
    communes = [str(value).strip() for value in criteria.get("comunas") or [] if str(value).strip()]
    types = [str(value).upper() for value in criteria.get("property_types") or [] if str(value).upper() in {"CASA", "DEPARTAMENTO"}]
    if not communes or not types:
        return {"status": "skipped", "reason": "verified_commune_and_type_required", "records": []}
    limits = job.get("cost_controls") or {}
    max_pages = max(1, min(int(limits.get("max_pages_per_source") or 2), 3))
    max_urls = max(1, min(int(limits.get("max_urls_per_source") or 30), 60))
    max_records = max(1, min(int(limits.get("max_records_per_source") or 30), max_urls))
    records: list[dict[str, Any]] = []
    failures = []
    # The existing TocToc discovery supports commune, operation, type and
    # publisher filters. Its discovery price bounds are deliberately ignored
    # by the scraper; we therefore verify price only from each detail result.
    segments = [(commune, kind) for commune in communes for kind in types]
    # These CLI limits apply per invocation. Bound the aggregate work across
    # all requested combinations, and disclose any segments left for a retry.
    selected = segments[:min(len(segments), max_pages, max_urls)]
    segment_count = max(1, len(selected))
    urls_per_segment = max(1, max_urls // segment_count)
    pages_per_segment = max(1, max_pages // segment_count)
    records_per_segment = max(1, max_records // segment_count)
    for commune, kind in selected:
        batch_id = "demand_" + uuid4().hex[:20]
        command = [sys.executable, str(ROOT / "scrapers" / "scraper_toctoc" / "run_toctoc.py"),
                   "run-full", "--operacion", str(criteria.get("operation") or "VENTA").lower(),
                   "--tipo", "casa" if kind == "CASA" else "departamento",
                   "--region", "metropolitana", "--comuna", commune.lower().replace(" ", "-"),
                   "--publicador", "2", "--max-pages", str(pages_per_segment),
                   "--max-urls", str(urls_per_segment), "--limit", str(records_per_segment),
                   "--batch-id", batch_id,
                   "--dry-run", "--no-llm", "--disable-post-distribution", "--reuse-html"]
        if isinstance(criteria.get("max_price_clp"), int) and criteria["max_price_clp"] > 0:
            # Discovery may ignore this filter; downstream matching and human
            # authorization still enforce the hard CLP ceiling.
            command.extend(["--precio-hasta", str(criteria["max_price_clp"])])
        try:
            output = _run(command, timeout=360)
            batch = _read_result(output, _PROCESSED_TOCTOC)
            records.extend(batch[:records_per_segment])
        except ScraperAdapterError as exc:
            failures.append({"commune": commune, "type": kind, "reason": str(exc)})
        finally:
            _cleanup_batch_artifacts("toctoc", batch_id)
    records = records[:max_records]
    return {"status": "partial" if failures else "completed", "records": records,
            "processed": len(records), "failures": failures,
            "coverage": {"requested_segments": len(segments),
                         "searched_segments": len(selected),
                         "omitted_segments": len(segments) - len(selected)},
            "filters_applied": {"communes": communes, "types": types,
                                "operation": criteria.get("operation"),
                                "publicador": "particular", "max_price_clp": False},
            "cache_policy": "shared_catalog_and_job_window",
            "bounded_pages_per_segment": max_pages,
            "bounded_urls_per_segment": max_urls}


def run_existing_scraper(source: str, job: dict) -> dict:
    if source == "yapo":
        return _yapo(job)
    if source == "toctoc":
        return _toctoc(job)
    raise ScraperAdapterError("unsupported_source")
