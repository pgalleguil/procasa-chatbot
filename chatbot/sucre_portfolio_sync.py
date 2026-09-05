"""Manual, listing-first portfolio synchronization for PROCASA SUCRE.

This module is deliberately independent from ``ficha_sync_loop``.  It uses the
Prop360 general listing as the change detector and only opens a full property
page for new or changed codes.  It never generates embeddings.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from pymongo import ReturnDocument

from config import Config
from scraping_convecta.scraping_prop360_ficha_completa import (
    COLLECTION_NAME,
    OFICINAS,
    Prop360Client,
    parse_listing_price,
    scrape_propiedad,
    upsert_ficha,
)

log = logging.getLogger("sucre.portfolio_sync")

SUCRE_OFFICE_ID = 7
SUCRE_OFFICE_NAME = OFICINAS[SUCRE_OFFICE_ID]
LISTING_PAGE_SIZE = 500
LOCK_COLLECTION = "portfolio_sync_locks"
STATUS_COLLECTION = "portfolio_sync_status"
SYNC_KEY = "prop360_sucre"
LOCK_LEASE_MINUTES = 90

# Exact fields returned by Prop360Client.fetch_listing().  They come from the
# listing table row; no detail/ficha fields are used by the detector.
LISTING_CONTROL_FIELDS = (
    "codigo",
    "precio",
    "estado",
    "operacion",
    "tipo",
    "comuna",
    "captador",
    "direccion",
    "region",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _norm_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip().casefold()
    return text or None


def _norm_price(value: Any) -> Any:
    text = _norm_text(value)
    if not text:
        return None
    # UF is the stable control value when present.  The CLP equivalent changes
    # daily and must not cause a false ficha scrape.
    uf, clp = parse_listing_price(str(value))
    if uf is not None:
        return ("uf", uf)
    if clp is not None:
        return ("clp", clp)
    return ("text", text)


def listing_fingerprint(row: dict) -> str:
    """Hash only the fields present in the general Prop360 listing."""
    payload = {}
    for field in LISTING_CONTROL_FIELDS:
        payload[field] = (
            _norm_price(row.get(field)) if field == "precio"
            else _norm_text(row.get(field))
        )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sucre_query() -> dict:
    return {
        "$or": [
            {"oficina_id": SUCRE_OFFICE_ID},
            {"oficina_nombre": SUCRE_OFFICE_NAME},
            {"resumen.oficina": SUCRE_OFFICE_NAME},
        ]
    }


def _empty_result(started_at: datetime) -> dict:
    return {
        "status": "running",
        "started_at": _iso(started_at),
        "finished_at": None,
        "duration_seconds": None,
        "office_id": SUCRE_OFFICE_ID,
        "office": SUCRE_OFFICE_NAME,
        "collection": COLLECTION_NAME,
        "prop360_total": 0,
        "mongo_total_before": 0,
        "nuevas": 0,
        "modificadas": 0,
        "sin_cambios": 0,
        "posibles_bajas": 0,
        "actualizadas": 0,
        "procesadas": 0,
        "errores": 0,
        "error_codes": [],
        "bajas_aplicadas": 0,
        "bajas_omitidas": False,
        "listing_fields": list(LISTING_CONTROL_FIELDS),
        "listing_meta": {},
        "run_id": None,
    }


def _persist_status(db, result: dict, status: str, **extra) -> None:
    payload = {"status": status, "updated_at": _iso(_utc_now()), **result, **extra}
    db[STATUS_COLLECTION].update_one(
        {"_id": SYNC_KEY}, {"$set": payload}, upsert=True
    )


def _acquire_lock(db, run_id: str, now: datetime) -> bool:
    locks = db[LOCK_COLLECTION]
    locks.update_one(
        {"_id": SYNC_KEY},
        {"$setOnInsert": {
            "status": "idle",
            "lease_until": datetime.fromtimestamp(0, tz=timezone.utc),
        }},
        upsert=True,
    )
    lease_until = now + timedelta(minutes=LOCK_LEASE_MINUTES)
    claimed = locks.find_one_and_update(
        {
            "_id": SYNC_KEY,
            "$or": [
                {"status": {"$ne": "running"}},
                {"lease_until": {"$lte": now}},
                {"lease_until": {"$exists": False}},
            ],
        },
        {"$set": {
            "status": "running",
            "run_id": run_id,
            "office_id": SUCRE_OFFICE_ID,
            "office": SUCRE_OFFICE_NAME,
            "started_at": _iso(now),
            "lease_until": lease_until,
        }},
        return_document=ReturnDocument.AFTER,
    )
    return bool(claimed and claimed.get("run_id") == run_id)


def _release_lock(db, run_id: str, status: str, finished_at: datetime) -> None:
    db[LOCK_COLLECTION].update_one(
        {"_id": SYNC_KEY, "run_id": run_id},
        {"$set": {
            "status": status,
            "finished_at": _iso(finished_at),
            "lease_until": finished_at,
        }},
    )


def _close_prop360_client(client) -> None:
    http_client = getattr(client, "client", None)
    close = getattr(http_client, "close", None)
    if callable(close):
        close()


def _validate_listing(rows: list[dict], meta: dict) -> tuple[bool, str | None]:
    if not meta or not meta.get("response_valid", True):
        return False, "invalid_listing_response"
    if not meta.get("complete", False):
        return False, "incomplete_listing_pagination"
    if not rows:
        return False, "empty_listing"
    codes = [str(row.get("codigo")).strip() for row in rows if row.get("codigo")]
    if len(codes) != len(rows):
        return False, "listing_row_without_code"
    if len(set(codes)) != len(codes):
        return False, "duplicate_listing_codes"
    reported_total = meta.get("reported_total")
    if reported_total is not None and len(rows) < int(reported_total):
        return False, "listing_below_reported_total"
    return True, None


def _existing_code_map(coll) -> dict[str, dict]:
    docs = coll.find(
        _sucre_query(),
        {"codigo": 1, "disponible_prop360": 1, "resumen": 1},
    )
    result = {}
    for doc in docs:
        if doc.get("codigo") is not None:
            result[str(doc["codigo"]).strip()] = doc
    return result


def _stored_fingerprint(doc: dict) -> str | None:
    resumen = doc.get("resumen") or {}
    fingerprint = resumen.get("listing_fingerprint")
    if fingerprint:
        return str(fingerprint)
    snapshot = resumen.get("snapshot_listado")
    return listing_fingerprint(snapshot) if isinstance(snapshot, dict) else None


def _finish_result(db, result: dict, status: str, started: datetime, run_id: str, **extra) -> dict:
    finished = _utc_now()
    result.update({
        "status": status,
        "finished_at": _iso(finished),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "run_id": run_id,
        **extra,
    })
    _persist_status(db, result, status)
    _release_lock(db, run_id, status, finished)
    return result


def run_sucre_portfolio_sync(
    *,
    dry_run: bool = False,
    apply_bajas: bool = False,
    db=None,
    prop360_client=None,
    client_factory: Callable[..., Any] | None = None,
) -> dict:
    """Synchronize only PROCASA SUCRE using listing-first change detection.

    ``apply_bajas`` defaults to False.  The function refuses to apply bajas
    unless login, response validation, code uniqueness and pagination checks
    all pass.  It never calls the embedding subsystem.
    """
    started = _utc_now()
    result = _empty_result(started)
    run_id = uuid.uuid4().hex
    mongo_client = None
    owned_prop360_client = prop360_client is None
    try:
        if db is None:
            from scraping_convecta.scraping_prop360_ficha_completa import get_mongo_collection

            mongo_client, coll = get_mongo_collection(COLLECTION_NAME)
            db = mongo_client[Config.DB_NAME]
        else:
            coll = db[COLLECTION_NAME]

        if not _acquire_lock(db, run_id, started):
            result.update({
                "status": "already_running",
                "finished_at": _iso(_utc_now()),
                "duration_seconds": 0.0,
                "run_id": None,
                "error": "already_running",
            })
            return result
        result["run_id"] = run_id
        _persist_status(db, result, "running")

        existing = _existing_code_map(coll)
        result["mongo_total_before"] = len(existing)

        if prop360_client is None:
            email = os.getenv("PROP360_EMAIL")
            password = os.getenv("PROP360_PASSWORD")
            factory = client_factory or Prop360Client
            prop360_client = factory(email, password, delay=0.3)
        if not prop360_client.login():
            return _finish_result(db, result, "failed", started, run_id, error="login_failed")

        rows = prop360_client.fetch_listing(SUCRE_OFFICE_ID)
        listing_meta = dict(getattr(prop360_client, "last_listing_meta", {}) or {})
        if not listing_meta:
            listing_meta = {
                "office_id": SUCRE_OFFICE_ID,
                "rows": len(rows),
                "pages": None,
                "complete": False,
                "response_valid": False,
            }
        result["listing_meta"] = listing_meta
        valid, validation_error = _validate_listing(rows, listing_meta)
        if not valid:
            result["errores"] = 1
            return _finish_result(
                db, result, "failed", started, run_id,
                error=validation_error,
                bajas_omitidas=True,
            )

        by_code = {str(row["codigo"]).strip(): row for row in rows}
        active = {
            code: row for code, row in by_code.items()
            if _norm_text(row.get("estado")) == "activa"
        }
        result["prop360_total"] = len(active)
        new_codes = sorted(code for code in active if code not in existing)
        changed_codes = sorted(
            code for code, row in active.items()
            if code in existing and _stored_fingerprint(existing[code]) != listing_fingerprint(row)
        )
        unchanged_codes = sorted(
            code for code in active
            if code in existing and code not in changed_codes
        )
        active_mongo_codes = {
            code for code, doc in existing.items()
            if bool(doc.get("disponible_prop360", True))
        }
        possible_bajas = sorted(active_mongo_codes - set(active))
        result.update({
            "nuevas": len(new_codes),
            "modificadas": len(changed_codes),
            "sin_cambios": len(unchanged_codes),
            "posibles_bajas": len(possible_bajas),
        })
        _persist_status(db, result, "running", classified_at=_iso(_utc_now()))

        for code in new_codes + changed_codes:
            result["procesadas"] += 1
            try:
                doc = scrape_propiedad(prop360_client, code, active[code])
                doc.setdefault("resumen", {})["listing_fingerprint"] = listing_fingerprint(active[code])
                if dry_run:
                    result["actualizadas"] += 1
                    continue
                nuevo, actualizado = upsert_ficha(coll, doc)
                if nuevo or actualizado:
                    result["actualizadas"] += 1
            except Exception:
                result["errores"] += 1
                result["error_codes"].append(code)
                log.exception("[SUCRE_SYNC] fallo procesando código %s", code)

        if apply_bajas and not dry_run and result["errores"] == 0:
            baja_result = coll.update_many(
                {
                    **_sucre_query(),
                    "disponible_prop360": True,
                    "codigo": {"$in": possible_bajas},
                },
                {"$set": {
                    "disponible_prop360": False,
                    "resumen.disponible_prop360": False,
                    "estado.disponible_prop360": False,
                    "fecha_baja_automatica": _iso(_utc_now()),
                    "baja_origen": "sucre_listing_sync",
                }},
            )
            result["bajas_aplicadas"] = int(getattr(baja_result, "modified_count", 0))
        elif possible_bajas:
            result["bajas_omitidas"] = True

        final_status = "failed" if result["errores"] else "completed"
        return _finish_result(
            db, result, final_status, started, run_id,
            bajas_omitidas=bool(result["bajas_omitidas"]),
        )
    except Exception as exc:
        log.exception("[SUCRE_SYNC] ciclo fallido")
        result["errores"] = max(1, result["errores"])
        if db is not None and result.get("run_id") == run_id:
            return _finish_result(db, result, "failed", started, run_id, error=type(exc).__name__)
        result.update({
            "status": "failed",
            "finished_at": _iso(_utc_now()),
            "duration_seconds": round((_utc_now() - started).total_seconds(), 3),
            "error": type(exc).__name__,
        })
        return result
    finally:
        if owned_prop360_client and prop360_client is not None:
            _close_prop360_client(prop360_client)
        if mongo_client is not None:
            mongo_client.close()


__all__ = [
    "LISTING_CONTROL_FIELDS",
    "SUCRE_OFFICE_ID",
    "SUCRE_OFFICE_NAME",
    "listing_fingerprint",
    "run_sucre_portfolio_sync",
]
