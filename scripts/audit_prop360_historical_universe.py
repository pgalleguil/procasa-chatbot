"""One-shot, read-only audit of the live Prop360 listing universe.

This module deliberately does not import the production runner functions that
write ``universo_cartera_prop360``.  Its only Mongo writes are the technical
lock/report document in ``prop360_audit_reports``.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from chatbot.storage import get_db
from scraping_convecta.scraping_prop360_ficha_completa import (
    BASE_URL,
    OFICINAS,
    PROPIEDADES_ASHX,
    Prop360AuthError,
    Prop360Client,
)

REPORT_ID = "historical_universe_v1"
REPORT_COLLECTION = "prop360_audit_reports"
AUDIT_OFFICES = [1, 2, 3, 5, 6, 7, 8]
MAX_DETAIL = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_number(value: Any) -> int | float | None:
    try:
        return float(value) if "." in str(value) else int(value)
    except (TypeError, ValueError):
        return None


def _redact_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None
    return text[:120]


def _listing_params(office_id: int, page: int) -> dict[str, Any]:
    return {
        "ac": "listadoPropiedades",
        "ofi": office_id,
        "op": 2,
        "pa": page,
        "nr": 500,
        "or": 1,
        "od": 2,
        "vi": 2,
        "ca": "10,1,2,3,4,5,6,7,8,9",
        "_": time.time() % 100,
    }


def _row_parts(row_html: str) -> list[str]:
    return re.split(r"</td><td[^>]*>", row_html)


def _field_coverage(row_htmls: list[str]) -> dict[str, dict[str, Any]]:
    definitions = {
        "codigo": lambda row: bool(re.search(r"rel=['\"]\d+['\"]", row)),
        "estado": lambda row: bool(re.search(r"lnkEditEstado'[^>]*>", row)),
        "tipo": lambda row: len(_row_parts(row)) > 2,
        "operacion": lambda row: bool(re.search(r"label label-sm label-", row)),
        "captador": lambda row: len(_row_parts(row)) > 5,
        "direccion": lambda row: len(_row_parts(row)) > 6,
        "precio": lambda row: len(_row_parts(row)) > 7,
        "comuna": lambda row: len(_row_parts(row)) > 8,
        "region": lambda row: len(_row_parts(row)) > 9,
        "dormitorios": lambda row: bool(re.search(r"dormitorio|habitaci", row, re.I)),
        "banos": lambda row: bool(re.search(r"baño|bano", row, re.I)),
        "superficie": lambda row: bool(re.search(r"superficie|m²|m2", row, re.I)),
        "fecha": lambda row: bool(re.search(r"fecha|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", row, re.I)),
        "oficina": lambda row: bool(re.search(r"oficina", row, re.I)),
    }
    result: dict[str, dict[str, Any]] = {}
    total = len(row_htmls)
    for name, predicate in definitions.items():
        count = sum(1 for row in row_htmls if predicate(row))
        result[name] = {"exists": count > 0, "coverage": (count / total if total else 0.0), "parser": name in {"codigo", "tipo", "operacion", "estado", "captador", "direccion", "precio", "comuna", "region"}}
    return result


def _sanitize_detail(detail: dict[str, Any]) -> dict[str, Any]:
    allowed = {"estado", "ultima_actualizacion", "estado_prop360", "disponible_prop360", "ingresado_el", "fecha_baja", "fecha_cierre", "motivo", "vendido", "arrendado", "retirado", "suspendido"}
    out = {}
    for key, value in detail.items():
        if key in allowed and not isinstance(value, (dict, list)):
            out[key] = _redact_text(value)
    return out


def _acquire_lock(coll, sha: str) -> bool:
    now = _now()
    try:
        result = coll.update_one(
            {"_id": REPORT_ID, "status": {"$nin": ["running", "completed"]}},
            {"$set": {"status": "running", "started_at": now, "sha": sha}},
            upsert=True,
        )
        return bool(result.modified_count or result.upserted_id)
    except Exception as exc:
        # A concurrent upsert can lose the unique _id race. Treat that as
        # another worker owning the lock; do not retry the audit.
        if exc.__class__.__name__ == "DuplicateKeyError":
            return False
        raise


def _fetch_raw(client: Prop360Client, office_id: int) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    raw_rows: list[str] = []
    status_counts: Counter[str] = Counter()
    page = 1
    while True:
        started = time.perf_counter()
        response = client._get(PROPIEDADES_ASHX, params=_listing_params(office_id, page))
        payload = response.json()
        listing_html = payload[0].get("listing", "") if isinstance(payload, list) and payload else ""
        page_rows = re.split(r"<tr id='filaProp\d+'>", listing_html)[1:]
        raw_rows.extend(page_rows)
        parsed = [client._parse_listing_row(row) for row in page_rows]
        rows.extend(parsed)
        for row in parsed:
            status_counts[(row.get("estado") or "<VACIO>").strip()] += 1
        client._wait()
        if len(page_rows) < 500:
            break
        page += 1
        if time.perf_counter() - started > 120:
            raise TimeoutError(f"listado oficina {office_id} excedió timeout de página")
    return rows, raw_rows, dict(status_counts)


def _aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_office: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    for row in rows:
        by_office[str(row.get("office_id") or "desconocida")] += 1
        by_status[(row.get("estado") or "<VACIO>").strip()] += 1
    return {"by_status": dict(by_status), "by_office": dict(by_office)}


def run_prop360_historical_audit() -> dict[str, Any]:
    """Run once in the production runtime; returns only aggregate evidence."""
    db = get_db()
    reports = db[REPORT_COLLECTION]
    sha = os.getenv("RENDER_GIT_COMMIT", "runtime")
    if not _acquire_lock(reports, sha):
        return reports.find_one({"_id": REPORT_ID}, {"_id": 0}) or {"status": "running"}

    started = time.perf_counter()
    login_requests = 0
    listing_requests = 0
    listing_bytes = 0
    detail_requests = 0
    detail_bytes = 0
    try:
        email = os.getenv("PROP360_EMAIL")
        password = os.getenv("PROP360_PASSWORD")
        if not email or not password:
            raise RuntimeError("production Prop360 environment unavailable")
        client = Prop360Client(email, password, delay=0.1)
        login_started = time.perf_counter()
        client.login()
        login_seconds = time.perf_counter() - login_started

        all_rows: list[dict[str, Any]] = []
        raw_sample: list[str] = []
        by_office: dict[str, Any] = {}
        for office_id in AUDIT_OFFICES:
            before = len(all_rows)
            rows, raw_rows, statuses = _fetch_raw(client, office_id)
            for row in rows:
                row["office_id"] = office_id
            all_rows.extend(rows)
            raw_sample.extend(raw_rows[:15])
            by_office[str(office_id)] = {"name": OFICINAS.get(office_id), "rows": len(rows), "by_status": statuses}
            listing_requests += max(1, (len(rows) + 499) // 500)
            listing_bytes += sum(len(r.encode("utf-8", "ignore")) for r in raw_rows)

        codes = [str(row.get("codigo")) for row in all_rows if row.get("codigo")]
        code_counts = Counter(codes)
        unique_codes = set(code_counts)
        statuses = Counter((row.get("estado") or "<VACIO>").strip() for row in all_rows)
        mongo_codes = {str(v) for v in db.universo_cartera_prop360.distinct("codigo") if v is not None}
        not_in_mongo = unique_codes - mongo_codes
        mongo_not_in_live = mongo_codes - unique_codes
        candidates = [row for row in all_rows if (row.get("estado") or "").strip() != "Activa"][:MAX_DETAIL]
        detail_results = []
        detail_started = time.perf_counter()
        for row in candidates:
            code = str(row["codigo"])
            html_edit = client.get_propeditar(code)
            html_state = client.get_estado(code)
            detail_requests += 2
            detail_bytes += len(html_edit.encode("utf-8", "ignore")) + len(html_state.encode("utf-8", "ignore"))
            detail_results.append({"office": row.get("office_id"), "type": row.get("tipo"), "commune": row.get("comuna"), "edit_fields": _field_coverage([html_edit]), "state_fields": _field_coverage([html_state])})
        detail_seconds = time.perf_counter() - detail_started

        report = {
            "total_raw": len(all_rows),
            "unique_codes": len(unique_codes),
            "duplicates": sum(v - 1 for v in code_counts.values() if v > 1),
            "by_status": dict(statuses),
            "by_office": by_office,
            "mongo_overlap": len(unique_codes & mongo_codes),
            "not_in_mongo": len(not_in_mongo),
            "mongo_not_in_live": len(mongo_not_in_live),
            "listing_fields": _field_coverage(raw_sample),
            "inactive_sample_size": len(candidates),
            "inactive_sample": detail_results,
            "ingresado_el": {"status": "requires_explicit_live_label_review"},
            "exit_date": {"status": "not_asserted_without_explicit_field"},
            "exit_reason": {"status": "not_asserted_without_explicit_field"},
            "id_republication_evidence": {"status": "not_inferred"},
            "network_cost": {
                "login_requests": login_requests,
                "listing_requests": listing_requests,
                "listing_bytes": listing_bytes,
                "listing_seconds": round(time.perf_counter() - started - detail_seconds, 3),
                "detail_requests": detail_requests,
                "detail_bytes": detail_bytes,
                "detail_seconds": round(detail_seconds, 3),
                "login_seconds": round(login_seconds, 3),
            },
            "conclusion": {"read_only": True, "max_detail": MAX_DETAIL, "full_detail_backfill": "not_recommended"},
        }
        reports.update_one({"_id": REPORT_ID}, {"$set": {"status": "completed", "completed_at": _now(), "report": report}}, upsert=False)
        return {"status": "completed", "report": report}
    except Exception as exc:
        reports.update_one({"_id": REPORT_ID}, {"$set": {"status": "failed", "completed_at": _now(), "error_type": type(exc).__name__}}, upsert=False)
        raise
