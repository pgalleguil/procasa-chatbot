"""Operational, listing-first portfolio synchronization for Prop360.

The operational flow has a deliberately narrow scope:

* new active properties are scraped completely;
* existing properties are compared only on published price and captador;
* price/captador changes are written directly without opening the ficha;
* active Mongo properties missing from the validated active listing are only
  reported as possible bajas unless ``apply_bajas=True``.

The general listing fingerprint remains available for future deep-review
tooling, but it never decides whether an existing property is scraped here.
This module is independent from ``ficha_sync_loop`` and never loads embeddings.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from pymongo import ReturnDocument

from config import Config
from scraping_convecta.scraping_prop360_ficha_completa import (
    COLLECTION_NAME,
    HISTORY_COLLECTION_NAME,
    OFICINAS,
    Prop360Client,
    append_history_event,
    audit_hash,
    build_history_event,
    deduplicate_historial_cambios,
    ensure_history_indexes,
    history_collection_for,
    normalize_numeric_for_compare,
    normalize_text_for_compare,
    normalize_published_amount,
    parse_listing_price,
    scrape_propiedad,
    upsert_ficha,
)

log = logging.getLogger("portfolio.operational_sync")

SUCRE_OFFICE_ID = 7
SUCRE_OFFICE_NAME = OFICINAS[SUCRE_OFFICE_ID]
AVAILABLE_OFFICE_IDS = tuple(
    sorted(office_id for office_id, name in OFICINAS.items() if name.startswith("PROCASA "))
)
LOCK_COLLECTION = "portfolio_sync_locks"
STATUS_COLLECTION = "portfolio_sync_status"
SYNC_KEY = "prop360_sucre"
LOCK_LEASE_MINUTES = 90
OPERATIONAL_LISTING_FIELDS = ("codigo", "precio", "captador", "estado")
HISTORY_SOURCE = "Prop360"

# Retained for compatibility and future deep review.  These fields are not
# used to decide whether an existing property is scraped operationally.
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

_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _norm_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip().casefold()
    return text or None


def _norm_number(value: Any) -> float | int | None:
    # Values coming from Mongo often arrive as 320000.0.  Treat actual
    # numeric types numerically; the legacy text normalizer is intentionally
    # reserved for textual representations such as "4.500".
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return normalize_numeric_for_compare(value)


def _same_value(left: Any, right: Any) -> bool:
    left_num = _norm_number(left)
    right_num = _norm_number(right)
    if left_num is not None and right_num is not None:
        return left_num == right_num
    return normalize_text_for_compare(left) == normalize_text_for_compare(right)


def _norm_price(value: Any) -> Any:
    text = _norm_text(value)
    if not text:
        return None
    uf, clp = parse_listing_price(str(value))
    if uf is not None:
        return ("uf", uf)
    if clp is not None:
        return ("clp", clp)
    return ("text", text)


def listing_fingerprint(row: dict) -> str:
    """Hash only fields present in the general Prop360 listing."""
    payload = {}
    for field in LISTING_CONTROL_FIELDS:
        payload[field] = (
            _norm_price(row.get(field))
            if field == "precio"
            else _norm_text(row.get(field))
        )
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def is_admin_user(user: dict | None) -> bool:
    """Only the administrator role may invoke the future CRM action."""
    return bool(user) and str(user.get("rol", "")).strip().lower() == "admin"


def _office_ids(office_id: int | None) -> tuple[int, ...]:
    if office_id is None:
        return AVAILABLE_OFFICE_IDS
    try:
        normalized = int(office_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("office_id debe ser un entero de OFICINAS o None") from exc
    if normalized not in AVAILABLE_OFFICE_IDS:
        raise ValueError(f"office_id no permitido: {normalized}")
    return (normalized,)


def _office_name(office_id: int) -> str:
    return OFICINAS[office_id]


def _scope_name(office_ids: tuple[int, ...]) -> str:
    return _office_name(office_ids[0]) if len(office_ids) == 1 else "TODAS PROCASA"


def _scope_key(office_ids: tuple[int, ...]) -> str:
    if len(office_ids) == 1 and office_ids[0] == SUCRE_OFFICE_ID:
        return SYNC_KEY
    return "prop360_operational_" + "_".join(str(item) for item in office_ids)


def _office_query(office_id: int | None) -> dict:
    clauses = []
    for current_id in _office_ids(office_id):
        name = _office_name(current_id)
        clauses.extend(
            (
                {"oficina_id": current_id},
                {"oficina_nombre": name},
                {"resumen.oficina": name},
            )
        )
    return {"$or": clauses}


def _sucre_query() -> dict:
    return _office_query(SUCRE_OFFICE_ID)


def _empty_result(started_at: datetime, office_ids: tuple[int, ...]) -> dict:
    return {
        "status": "running",
        "started_at": _iso(started_at),
        "finished_at": None,
        "duration_seconds": None,
        "office_id": office_ids[0] if len(office_ids) == 1 else None,
        "office": _scope_name(office_ids),
        "offices": [
            {"office_id": item, "office": _office_name(item)} for item in office_ids
        ],
        "collection": COLLECTION_NAME,
        "history_collection": HISTORY_COLLECTION_NAME,
        "prop360_total": 0,
        "prop360_active": 0,
        "mongo_total_before": 0,
        "mongo_active_before": 0,
        "nuevas": 0,
        "cambios_precio": 0,
        "cambios_ejecutivo": 0,
        "cambios_precio_ejecutivo": 0,
        "precios_ambiguos": 0,
        "sin_cambios_operativos": 0,
        "posibles_bajas": 0,
        "fichas_completas_requeridas": 0,
        "fichas_completas_consultadas": 0,
        "actualizadas": 0,
        "procesadas": 0,
        "errores": 0,
        "errors": 0,
        "error_codes": [],
        "bajas_aplicadas": 0,
        "bajas_omitidas": False,
        "listing_fields": list(OPERATIONAL_LISTING_FIELDS),
        "listing_meta": {},
        "operational_change_samples": [],
        "possible_baja_codes": [],
        "precio_ambiguo_codes": [],
        "reactivaciones": 0,
        "dry_run": False,
        "apply_bajas": False,
        "login": "not_attempted",
        "run_id": None,
    }


def _get_local_lock(key: str) -> threading.Lock:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.Lock())


def _acquire_local_lock(key: str) -> bool:
    return _get_local_lock(key).acquire(blocking=False)


def _release_local_lock(key: str) -> None:
    lock = _get_local_lock(key)
    if lock.locked():
        lock.release()


def _persist_status(db, result: dict, status: str, sync_key: str, **extra) -> None:
    payload = {
        "status": status,
        "updated_at": _iso(_utc_now()),
        "sync_key": sync_key,
        **result,
        **extra,
    }
    db[STATUS_COLLECTION].update_one(
        {"_id": sync_key}, {"$set": payload}, upsert=True
    )


def _acquire_lock(
    db,
    run_id: str,
    now: datetime,
    sync_key: str = SYNC_KEY,
    office_ids: tuple[int, ...] = (SUCRE_OFFICE_ID,),
) -> bool:
    locks = db[LOCK_COLLECTION]
    locks.update_one(
        {"_id": sync_key},
        {
            "$setOnInsert": {
                "status": "idle",
                "lease_until": datetime.fromtimestamp(0, tz=timezone.utc),
            }
        },
        upsert=True,
    )
    lease_until = now + timedelta(minutes=LOCK_LEASE_MINUTES)
    claimed = locks.find_one_and_update(
        {
            "_id": sync_key,
            "$or": [
                {"status": {"$ne": "running"}},
                {"lease_until": {"$lte": now}},
                {"lease_until": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "status": "running",
                "run_id": run_id,
                "office_ids": list(office_ids),
                "office": _scope_name(office_ids),
                "started_at": _iso(now),
                "lease_until": lease_until,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    return bool(claimed and claimed.get("run_id") == run_id)


def _release_lock(
    db,
    run_id: str,
    status: str,
    finished_at: datetime,
    sync_key: str = SYNC_KEY,
) -> None:
    db[LOCK_COLLECTION].update_one(
        {"_id": sync_key, "run_id": run_id},
        {
            "$set": {
                "status": status,
                "finished_at": _iso(finished_at),
                "lease_until": finished_at,
            }
        },
    )


def _close_prop360_client(client) -> None:
    http_client = getattr(client, "client", None)
    close = getattr(http_client, "close", None)
    if callable(close):
        close()


def _validate_listing(
    rows: list[dict], meta: dict, expected_office_id: int | None = None
) -> tuple[bool, str | None]:
    if not meta or not meta.get("response_valid", True):
        return False, "invalid_listing_response"
    if expected_office_id is not None and meta.get("office_id") != expected_office_id:
        return False, "wrong_listing_office"
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


def _existing_code_map(coll, office_id: int | None = SUCRE_OFFICE_ID) -> dict[str, dict]:
    docs = coll.find(
        _office_query(office_id),
        {
            "codigo": 1,
            "oficina_id": 1,
            "oficina_nombre": 1,
            "disponible_prop360": 1,
            "resumen": 1,
            "estado": 1,
            "tipo_operacion": 1,
            "historial_cambios": 1,
            "audit_hash": 1,
            "versiones": 1,
            "metadata": 1,
        },
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


def _stored_listing_snapshot(doc: dict) -> dict | None:
    snapshot = (doc.get("resumen") or {}).get("snapshot_listado")
    return snapshot if isinstance(snapshot, dict) else None


def _stored_executive(doc: dict) -> Any:
    resumen = doc.get("resumen") or {}
    if "ejecutivo" in resumen:
        return resumen.get("ejecutivo")
    estado = doc.get("estado") or {}
    if "ejecutivo" in estado:
        return estado.get("ejecutivo")
    return doc.get("ejecutivo")


def _stored_prices(doc: dict) -> tuple[Any, Any]:
    resumen = doc.get("resumen") or {}
    uf = resumen.get("precio_uf", doc.get("precio_uf"))
    clp = resumen.get("precio_clp", doc.get("precio_clp"))
    tipo_operacion = doc.get("tipo_operacion") or {}
    for operation in ("precio_venta", "precio_arriendo"):
        nested = tipo_operacion.get(operation) or {}
        if uf is None:
            uf = nested.get("precio_uf")
        if clp is None:
            clp = nested.get("precio_clp")
    return uf, clp


def _parse_publication_price(value: Any, default_currency: str | None = None) -> dict | None:
    """Return the original published currency and amount from a listing cell."""
    if value is None or not str(value).strip():
        return None
    if isinstance(value, dict):
        currency = str(
            value.get("moneda")
            or value.get("moneda_publicacion")
            or value.get("moneda_publicada")
            or ""
        ).upper()
        amount = value.get("monto", value.get("monto_publicacion", value.get("precio_publicado")))
        if currency in {"UF", "CLP"} and amount is not None:
            return {
                "moneda": currency,
                "monto": normalize_published_amount(amount),
                "uf": normalize_published_amount(amount) if currency == "UF" else None,
                "clp": normalize_published_amount(amount) if currency == "CLP" else None,
            }
    text = str(value).strip()
    uf, clp = parse_listing_price(text)
    default_currency = str(default_currency or "").upper()
    if default_currency == "UF" and uf is not None:
        return {"moneda": "UF", "monto": uf, "uf": uf, "clp": clp}
    if default_currency == "CLP" and clp is not None:
        return {"moneda": "CLP", "monto": clp, "uf": uf, "clp": clp}
    if uf is not None and clp is None:
        return {"moneda": "UF", "monto": uf, "uf": uf, "clp": None}
    if clp is not None and uf is None:
        return {"moneda": "CLP", "monto": clp, "uf": None, "clp": clp}
    # A bare number, or a cell with both currencies but no canonical context,
    # is intentionally not assigned a currency.  The caller reports it as
    # PRECIO_AMBIGUO instead of contaminating the operational history.
    return None


def _parse_primary_listing_price(value: Any) -> dict | None:
    """Parse the publication amount shown first in a listing cell.

    Legacy printable-ficha documents captured the secondary converted amount
    instead of the publication amount.  Their listing snapshot preserves the
    display order, where the first explicit ``UF`` or ``$`` amount is the
    published currency.  This helper is only used for that legacy recovery
    path; canonical documents continue to use their stored publication
    currency.
    """
    if value is None or not str(value).strip():
        return None
    text = " ".join(str(value).split())
    match = re.search(r"(?P<uf>\bUF\s*[\d.,]+)|(?P<clp>\$\s*[\d.,]+)", text, re.I)
    if match:
        currency = "UF" if match.group("uf") is not None else "CLP"
        raw_amount = match.group("uf") or match.group("clp")
        raw_amount = re.sub(r"^\s*(?:UF|\$)\s*", "", raw_amount, flags=re.I)
        amount = normalize_published_amount(raw_amount)
        if amount is None:
            return None
        uf, clp = parse_listing_price(text)
        return {
            "moneda": currency,
            "monto": amount,
            "uf": uf,
            "clp": clp,
        }
    return None


def _is_legacy_printable_doc(doc: dict) -> bool:
    metadata = doc.get("metadata") or {}
    resumen = doc.get("resumen") or {}
    return (
        metadata.get("origen_ficha") == "ficha_imprimible"
        and not resumen.get("moneda_publicacion")
        and resumen.get("monto_publicacion") is None
    )


def _legacy_stored_publication_amount(doc: dict) -> Any:
    """Return the amount captured by the old printable-ficha parser."""
    for operation in ("precio_venta", "precio_arriendo"):
        nested = (doc.get("tipo_operacion") or {}).get(operation)
        if isinstance(nested, dict):
            for field in ("precio_publicado", "precio_clp", "precio_uf"):
                if nested.get(field) is not None:
                    return nested.get(field)
    resumen = doc.get("resumen") or {}
    for field in ("precio_clp", "precio_uf"):
        if resumen.get(field) is not None:
            return resumen.get(field)
    return None


def _legacy_snapshot_publication_price(doc: dict) -> dict | None:
    """Recover the original currency from a legacy listing snapshot.

    The old printable parser persisted the secondary amount.  Matching that
    persisted value against the two explicit snapshot amounts identifies
    which one was secondary, without relying on magnitude or a percentage
    threshold.
    """
    if not _is_legacy_printable_doc(doc):
        return None
    snapshot = (doc.get("resumen") or {}).get("snapshot_listado")
    if not isinstance(snapshot, dict):
        return None
    parsed = parse_listing_price(snapshot.get("precio"))
    if parsed[0] is None or parsed[1] is None:
        return None
    stored_amount = _legacy_stored_publication_amount(doc)
    if stored_amount is None:
        return None
    if _same_value(stored_amount, parsed[1]):
        return {"moneda": "UF", "monto": parsed[0], "uf": parsed[0], "clp": parsed[1]}
    if _same_value(stored_amount, parsed[0]):
        return {"moneda": "CLP", "monto": parsed[1], "uf": parsed[0], "clp": parsed[1]}
    return None


def _known_publication_currency(doc: dict) -> str | None:
    resumen = doc.get("resumen") or {}
    currency = str(resumen.get("moneda_publicacion") or "").upper()
    if currency in {"UF", "CLP"}:
        return currency
    if _is_legacy_printable_doc(doc):
        recovered = _legacy_snapshot_publication_price(doc)
        if recovered:
            return recovered["moneda"]
    # Older documents may not have the summary fields, but the ficha parser
    # preserved the source currency alongside precio_publicado.
    for operation in ("precio_venta", "precio_arriendo"):
        nested = (doc.get("tipo_operacion") or {}).get(operation) or {}
        currency = str(nested.get("moneda_publicada") or "").upper()
        if currency in {"UF", "CLP"}:
            return currency
    snapshot = resumen.get("snapshot_listado")
    if isinstance(snapshot, dict):
        parsed = _parse_publication_price(snapshot.get("precio"))
        if parsed:
            return parsed["moneda"]
    if (
        (resumen.get("precio_uf") is not None and resumen.get("precio_clp") is None)
        or (doc.get("precio_uf") is not None and doc.get("precio_clp") is None)
    ):
        return "UF"
    if (
        (resumen.get("precio_clp") is not None and resumen.get("precio_uf") is None)
        or (doc.get("precio_clp") is not None and doc.get("precio_uf") is None)
    ):
        return "CLP"
    return None


def _stored_publication_price(doc: dict, listed: dict | None = None) -> dict | None:
    resumen = doc.get("resumen") or {}
    currency = str(resumen.get("moneda_publicacion") or "").upper() or None
    amount = resumen.get("monto_publicacion")
    uf, clp = _stored_prices(doc)
    if currency in {"UF", "CLP"}:
        if amount is None:
            amount = uf if currency == "UF" else clp
        return {"moneda": currency, "monto": amount, "uf": uf, "clp": clp}

    # Recover the previous commercial amount from the legacy listing
    # snapshot.  The nested ``moneda_publicada`` fields in these documents
    # may describe the converted secondary amount because the old printable
    # parser selected the wrong HTML label.
    if _is_legacy_printable_doc(doc):
        recovered = _legacy_snapshot_publication_price(doc)
        if recovered:
            return recovered

    # This is the canonical source for legacy ficha documents.  The other
    # currency in the same object is a derived conversion and must not win the
    # commercial-price comparison.
    for operation in ("precio_venta", "precio_arriendo"):
        nested = (doc.get("tipo_operacion") or {}).get(operation) or {}
        nested_currency = str(nested.get("moneda_publicada") or "").upper()
        nested_amount = nested.get("precio_publicado")
        if nested_currency in {"UF", "CLP"} and nested_amount is not None:
            return {
                "moneda": nested_currency,
                "monto": nested_amount,
                "uf": uf,
                "clp": clp,
            }

    snapshot = resumen.get("snapshot_listado")
    if isinstance(snapshot, dict):
        snapshot_price = _parse_publication_price(snapshot.get("precio"))
        if snapshot_price is not None:
            snapshot_amount = snapshot_price["monto"]
            if snapshot_price["moneda"] == "UF" and uf is not None:
                snapshot_amount = uf
            elif snapshot_price["moneda"] == "CLP" and clp is not None:
                snapshot_amount = clp
            return {
                "moneda": snapshot_price["moneda"],
                "monto": snapshot_amount,
                "uf": uf,
                "clp": clp,
            }
    if uf is not None and clp is None:
        return {"moneda": "UF", "monto": uf, "uf": uf, "clp": clp}
    if clp is not None and uf is None:
        return {"moneda": "CLP", "monto": clp, "uf": uf, "clp": clp}
    return None


def _price_change(doc: dict, row: dict) -> dict | None:
    return _price_assessment(doc, row)["change"]


def _price_assessment(doc: dict, row: dict) -> dict:
    raw_price = row.get("precio")
    stored = _stored_publication_price(doc)
    if _is_legacy_printable_doc(doc):
        listed = _parse_publication_price(
            raw_price,
            default_currency=(stored or {}).get("moneda"),
        )
    else:
        listed = _parse_publication_price(
            raw_price, default_currency=_known_publication_currency(doc)
        )
    if listed is None or listed.get("monto") is None:
        return {
            "change": None,
            "ambiguous": bool(_norm_text(raw_price)),
        }
    if stored is not None and stored.get("moneda") == listed.get("moneda"):
        if _same_value(stored.get("monto"), listed.get("monto")):
            return {"change": None, "ambiguous": False}
    elif stored is None:
        return {"change": {
            "campo": "precio_publicado",
            "moneda_anterior": None,
            "monto_anterior": None,
            "moneda_nueva": listed["moneda"],
            "monto_nuevo": listed["monto"],
            "anterior": None,
            "nuevo": listed["monto"],
        }, "ambiguous": False}
    return {"change": {
        "campo": "precio_publicado",
        "moneda_anterior": stored.get("moneda") if stored else None,
        "monto_anterior": stored.get("monto") if stored else None,
        "moneda_nueva": listed["moneda"],
        "monto_nuevo": listed["monto"],
        "anterior": stored.get("monto") if stored else None,
        "nuevo": listed["monto"],
    }, "ambiguous": False}


def _executive_change(doc: dict, row: dict) -> dict | None:
    listed = row.get("captador")
    if _norm_text(listed) is None:
        return None
    stored = _stored_executive(doc)
    if _same_value(stored, listed):
        return None
    return {"campo": "ejecutivo", "mongo": stored, "prop360": listed}


def _operation_keys(row: dict) -> tuple[str, ...]:
    operation = _norm_text(row.get("operacion")) or ""
    keys = []
    if "venta" in operation or "vende" in operation:
        keys.append("precio_venta")
    if "arriendo" in operation or "arrienda" in operation:
        keys.append("precio_arriendo")
    return tuple(keys)


def _append_compat_history(existing: dict, entries: list[dict]) -> list[dict]:
    """Preserve the legacy field while allowing repeated real transitions."""
    history = [item for item in (existing.get("historial_cambios") or []) if isinstance(item, dict)]
    for entry in entries:
        if _same_value(entry.get("valor_anterior"), entry.get("valor_nuevo")):
            continue
        history.append(
            {
                "fecha": entry["fecha"],
                "campo": entry["campo"],
                "valor_anterior": entry.get("valor_anterior"),
                "valor_nuevo": entry.get("valor_nuevo"),
            }
        )
    return history


def _set_dotted(document: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    current = document
    for part in parts[:-1]:
        if not isinstance(current.get(part), dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


def _state_hash_after(existing: dict, set_ops: dict) -> str:
    candidate = copy.deepcopy(existing)
    candidate.pop("audit_hash", None)
    candidate.pop("ultima_version_hash", None)
    candidate.pop("ultima_version_at", None)
    candidate.pop("fecha_baja_automatica", None)
    candidate.pop("baja_origen", None)
    candidate.pop("fecha_reactivacion", None)
    resumen = candidate.get("resumen")
    if isinstance(resumen, dict):
        resumen.pop("ultima_actualizacion_operativa", None)
        resumen.pop("fecha_reactivacion_operativa", None)
    for path, value in set_ops.items():
        if path in {
            "audit_hash",
            "historial_cambios",
            "fecha_baja_automatica",
            "baja_origen",
            "fecha_reactivacion",
            "resumen.ultima_actualizacion_operativa",
            "resumen.fecha_reactivacion_operativa",
        }:
            continue
        _set_dotted(candidate, path, value)
    return audit_hash(candidate)


def _operational_update(
    existing: dict,
    row: dict,
    price_change: dict | None,
    executive_change: dict | None,
    reactivation: bool,
    office_id: int,
    sync_run_id: str,
    detected_at: str,
) -> tuple[dict, list[dict]]:
    set_ops: dict[str, Any] = {}
    compat_entries = []
    events_spec = []

    if price_change:
        listed = _parse_publication_price(
            row.get("precio"), default_currency=price_change["moneda_nueva"]
        )
        listed_uf = listed.get("uf") if listed else None
        listed_clp = listed.get("clp") if listed else None
        if listed:
            set_ops["resumen.moneda_publicacion"] = listed["moneda"]
            set_ops["resumen.monto_publicacion"] = listed["monto"]
        if listed_uf is not None:
            set_ops["resumen.precio_uf"] = listed_uf
        if listed_clp is not None:
            set_ops["resumen.precio_clp"] = listed_clp
        for operation in _operation_keys(row):
            nested = (existing.get("tipo_operacion") or {}).get(operation)
            if not isinstance(nested, dict):
                continue
            if listed_uf is not None:
                set_ops[f"tipo_operacion.{operation}.precio_uf"] = listed_uf
            if listed_clp is not None:
                set_ops[f"tipo_operacion.{operation}.precio_clp"] = listed_clp
            set_ops[f"tipo_operacion.{operation}.moneda_publicada"] = listed["moneda"]
            set_ops[f"tipo_operacion.{operation}.precio_publicado"] = listed["monto"]
            derived_amount = listed_clp if listed["moneda"] == "UF" else listed_uf
            derived_currency = "CLP" if listed["moneda"] == "UF" else "UF"
            if derived_amount is not None:
                set_ops[f"tipo_operacion.{operation}.precio_derivado"] = derived_amount
                set_ops[f"tipo_operacion.{operation}.precio_derivado_moneda"] = derived_currency
        if price_change["moneda_anterior"] == price_change["moneda_nueva"]:
            compat_field = "precio_uf" if price_change["moneda_nueva"] == "UF" else "precio_clp"
            compat_old = price_change["monto_anterior"]
            compat_new = price_change["monto_nuevo"]
        else:
            compat_field = "precio_publicado"
            compat_old = {
                "moneda": price_change["moneda_anterior"],
                "monto": price_change["monto_anterior"],
            }
            compat_new = {
                "moneda": price_change["moneda_nueva"],
                "monto": price_change["monto_nuevo"],
            }
        compat_entries.append(
            {
                "fecha": detected_at,
                "campo": compat_field,
                "valor_anterior": compat_old,
                "valor_nuevo": compat_new,
            }
        )
        events_spec.append(
            {
                "campo": "precio_publicado",
                "valor_anterior": {
                    "moneda": price_change["moneda_anterior"],
                    "monto": price_change["monto_anterior"],
                },
                "valor_nuevo": {
                    "moneda": price_change["moneda_nueva"],
                    "monto": price_change["monto_nuevo"],
                },
                "tipo_evento": "cambio_precio",
                "moneda_anterior": price_change["moneda_anterior"],
                "monto_anterior": price_change["monto_anterior"],
                "moneda_nueva": price_change["moneda_nueva"],
                "monto_nuevo": price_change["monto_nuevo"],
            }
        )

    if executive_change:
        new_executive = executive_change["prop360"]
        # resumen.ejecutivo is canonical; estado.ejecutivo is mirrored only
        # when that existing model section already contains the field.
        set_ops["resumen.ejecutivo"] = new_executive
        if isinstance(existing.get("estado"), dict) and "ejecutivo" in existing["estado"]:
            set_ops["estado.ejecutivo"] = new_executive
        compat_entries.append(
            {
                "fecha": detected_at,
                "campo": "ejecutivo",
                "valor_anterior": executive_change["mongo"],
                "valor_nuevo": new_executive,
            }
        )
        events_spec.append(
            {
                "campo": "ejecutivo",
                "valor_anterior": executive_change["mongo"],
                "valor_nuevo": new_executive,
                "tipo_evento": "cambio_ejecutivo",
            }
        )

    if reactivation:
        set_ops["disponible_prop360"] = True
        set_ops["resumen.disponible_prop360"] = True
        set_ops["resumen.estado_prop360"] = "Activa"
        if isinstance(existing.get("estado"), dict) and "disponible_prop360" in existing["estado"]:
            set_ops["estado.disponible_prop360"] = True
        if isinstance(existing.get("estado"), dict):
            set_ops["estado.estado_prop360"] = "Activa"
        set_ops["resumen.fecha_reactivacion_operativa"] = detected_at
        events_spec.append(
            {
                "campo": "estado_prop360",
                "valor_anterior": {
                    "estado_prop360": "Pasiva",
                    "disponible_prop360": False,
                },
                "valor_nuevo": {
                    "estado_prop360": "Activa",
                    "disponible_prop360": True,
                },
                "tipo_evento": "reactivacion",
            }
        )

    if not set_ops:
        return {}, []
    set_ops["resumen.ultima_actualizacion_operativa"] = detected_at
    set_ops["historial_cambios"] = _append_compat_history(existing, compat_entries)
    hash_anterior = existing.get("audit_hash")
    hash_nuevo = _state_hash_after(existing, set_ops)
    set_ops["audit_hash"] = hash_nuevo

    events = []
    oficina = existing.get("oficina_nombre") or (existing.get("resumen") or {}).get("oficina") or _office_name(office_id)
    codigo = existing.get("codigo")
    for spec in events_spec:
        events.append(
            build_history_event(
                codigo=codigo,
                oficina_id=office_id,
                oficina=oficina,
                campo=spec["campo"],
                valor_anterior=spec["valor_anterior"],
                valor_nuevo=spec["valor_nuevo"],
                tipo_evento=spec["tipo_evento"],
                hash_anterior=hash_anterior,
                hash_nuevo=hash_nuevo,
                sync_run_id=sync_run_id,
                fuente=HISTORY_SOURCE,
                fecha=detected_at,
                moneda_anterior=spec.get("moneda_anterior"),
                monto_anterior=spec.get("monto_anterior"),
                moneda_nueva=spec.get("moneda_nueva"),
                monto_nuevo=spec.get("monto_nuevo"),
            )
        )
    return set_ops, events


def _baja_update(
    existing: dict,
    office_id: int,
    sync_run_id: str,
    detected_at: str,
) -> tuple[dict, dict]:
    resumen = existing.get("resumen") or {}
    estado = existing.get("estado") or {}
    estado_anterior = (
        estado.get("estado_prop360")
        or resumen.get("estado_prop360")
        or "Activa"
    )
    set_ops = {
        "oficina_id": existing.get("oficina_id") or office_id,
        "oficina_nombre": existing.get("oficina_nombre") or _office_name(office_id),
        "disponible_prop360": False,
        "resumen.disponible_prop360": False,
        "resumen.estado_prop360": "Pasiva",
        "estado.disponible_prop360": False,
        "estado.estado_prop360": "Pasiva",
        "fecha_baja_automatica": detected_at,
        "baja_origen": "prop360_operational_listing",
        "resumen.ultima_actualizacion_operativa": detected_at,
    }
    set_ops["audit_hash"] = _state_hash_after(existing, set_ops)
    oficina_id_value = existing.get("oficina_id") or office_id
    oficina = existing.get("oficina_nombre") or (existing.get("resumen") or {}).get("oficina") or _office_name(office_id)
    event = build_history_event(
        codigo=existing.get("codigo"),
        oficina_id=oficina_id_value,
        oficina=oficina,
        campo="estado_prop360",
        valor_anterior={
            "estado_prop360": estado_anterior,
            "disponible_prop360": True,
        },
        valor_nuevo={
            "estado_prop360": "Pasiva",
            "disponible_prop360": False,
        },
        tipo_evento="baja",
        hash_anterior=existing.get("audit_hash"),
        hash_nuevo=set_ops["audit_hash"],
        sync_run_id=sync_run_id,
        fuente=HISTORY_SOURCE,
        fecha=detected_at,
    )
    set_ops["historial_cambios"] = _append_compat_history(
        existing,
        [
            {
                "fecha": detected_at,
                "campo": "estado_prop360",
                "valor_anterior": estado_anterior,
                "valor_nuevo": "Pasiva",
            },
            {
                "fecha": detected_at,
                "campo": "disponible_prop360",
                "valor_anterior": True,
                "valor_nuevo": False,
            }
        ],
    )
    return set_ops, event


def _write_events_and_update(coll, history_coll, events: list[dict], update_filter: dict, set_ops: dict):
    """Persist a direct operational update and its events atomically on Atlas."""
    database = getattr(coll, "database", None)
    mongo_client = getattr(database, "client", None)
    start_session = getattr(mongo_client, "start_session", None)
    if callable(start_session):
        try:
            with mongo_client.start_session() as session:
                with session.start_transaction():
                    for event in events:
                        history_coll.update_one(
                            {"_id": event["_id"]},
                            {"$setOnInsert": event},
                            upsert=True,
                            session=session,
                        )
                    return coll.update_one(
                        update_filter, {"$set": set_ops}, session=session
                    )
        except NotImplementedError:
            # mongomock exposes start_session but does not implement it.
            pass
    for event in events:
        append_history_event(history_coll, event)
    return coll.update_one(update_filter, {"$set": set_ops})


def _classify_scope(
    office_id: int,
    rows: list[dict],
    meta: dict,
    existing: dict[str, dict],
) -> tuple[dict, str | None]:
    valid, error = _validate_listing(rows, meta, expected_office_id=office_id)
    if not valid:
        return {"valid": False, "error": error}, error

    by_code = {str(row["codigo"]).strip(): row for row in rows}
    active = {
        code: row
        for code, row in by_code.items()
        if _norm_text(row.get("estado")) == "activa"
    }
    new_codes = sorted(code for code in active if code not in existing)
    price_only = []
    executive_only = []
    price_and_executive = []
    unchanged = []
    ambiguous_prices = []
    reactivations = []
    updates = []
    samples = []

    for code, row in active.items():
        if code not in existing:
            continue
        doc = existing[code]
        price_assessment = _price_assessment(doc, row)
        price_change = price_assessment["change"]
        price_ambiguous = price_assessment["ambiguous"]
        executive_change = _executive_change(doc, row)
        reactivation = not bool(doc.get("disponible_prop360", True))
        if price_ambiguous:
            ambiguous_prices.append(code)
        if price_change and executive_change:
            classification = "CAMBIO_PRECIO_Y_EJECUTIVO"
            price_and_executive.append(code)
        elif price_change:
            classification = "CAMBIO_PRECIO"
            price_only.append(code)
        elif executive_change:
            classification = "CAMBIO_EJECUTIVO"
            executive_only.append(code)
        elif reactivation:
            classification = "REACTIVACION"
            reactivations.append(code)
        elif price_ambiguous:
            classification = "PRECIO_AMBIGUO"
        else:
            classification = "SIN_CAMBIOS_OPERATIVOS"
            unchanged.append(code)

        if price_change or executive_change or reactivation or price_ambiguous:
            updates.append(
                {
                    "office_id": office_id,
                    "office": _office_name(office_id),
                    "codigo": code,
                    "classification": classification,
                    "price_change": price_change,
                    "price_ambiguous": price_ambiguous,
                    "executive_change": executive_change,
                    "reactivation": reactivation,
                }
            )
            if len(samples) < 20 and (price_change or executive_change or price_ambiguous):
                changes = {}
                if price_change:
                    changes["precio_publicado"] = {
                        "moneda_mongo": price_change["moneda_anterior"],
                        "monto_mongo": price_change["monto_anterior"],
                        "moneda_prop360": price_change["moneda_nueva"],
                        "monto_prop360": price_change["monto_nuevo"],
                    }
                if executive_change:
                    changes["ejecutivo"] = {
                        "mongo": executive_change["mongo"],
                        "prop360": executive_change["prop360"],
                    }
                if price_ambiguous:
                    changes["precio_publicado"] = {
                        "clasificacion": "PRECIO_AMBIGUO",
                        "valor_listado": row.get("precio"),
                    }
                samples.append(
                    {
                        "office_id": office_id,
                        "office": _office_name(office_id),
                        "codigo": code,
                        "classification": classification,
                        "changes": changes,
                    }
                )

    active_mongo_codes = {
        code
        for code, doc in existing.items()
        if bool(doc.get("disponible_prop360", True))
    }
    possible_bajas = sorted(active_mongo_codes - set(active))
    return {
        "valid": True,
        "office_id": office_id,
        "office": _office_name(office_id),
        "rows": by_code,
        "active": active,
        "new_codes": new_codes,
        "price_only": price_only,
        "executive_only": executive_only,
        "price_and_executive": price_and_executive,
        "unchanged": unchanged,
        "ambiguous_prices": ambiguous_prices,
        "reactivations": reactivations,
        "updates": updates,
        "samples": samples,
        "possible_bajas": possible_bajas,
    }, None


def _add_error(result: dict, code: str | None = None) -> None:
    result["errores"] += 1
    result["errors"] = result["errores"]
    if code and code not in result["error_codes"]:
        result["error_codes"].append(code)


def _finish_result(
    db,
    result: dict,
    status: str,
    started: datetime,
    run_id: str,
    sync_key: str,
    persist_state: bool,
    **extra,
) -> dict:
    finished = _utc_now()
    result.update(
        {
            "status": status,
            "finished_at": _iso(finished),
            "duration_seconds": round((finished - started).total_seconds(), 3),
            "run_id": run_id,
            **extra,
        }
    )
    result["errors"] = result["errores"]
    if persist_state:
        _persist_status(db, result, status, sync_key)
        _release_lock(db, run_id, status, finished, sync_key)
    return result


def run_portfolio_operational_sync(
    *,
    office_id: int | None = None,
    dry_run: bool = False,
    apply_bajas: bool = False,
    db=None,
    prop360_client=None,
    client_factory: Callable[..., Any] | None = None,
    persist_state: bool | None = None,
) -> dict:
    """Run the operational sync for one office or all PROCASA offices."""
    started = _utc_now()
    office_ids = _office_ids(office_id)
    sync_key = _scope_key(office_ids)
    result = _empty_result(started, office_ids)
    result["dry_run"] = dry_run
    result["apply_bajas"] = apply_bajas
    if persist_state is None:
        persist_state = not dry_run

    run_id = uuid.uuid4().hex
    mongo_client = None
    owned_prop360_client = prop360_client is None
    local_lock_held = False
    persistent_lock_held = False

    try:
        if db is None:
            from scraping_convecta.scraping_prop360_ficha_completa import get_mongo_collection

            mongo_client, coll = get_mongo_collection(COLLECTION_NAME)
            db = mongo_client[Config.DB_NAME]
        else:
            coll = db[COLLECTION_NAME]

        history_coll = history_collection_for(coll)
        if persist_state:
            persistent_lock_held = _acquire_lock(
                db, run_id, started, sync_key=sync_key, office_ids=office_ids
            )
        else:
            local_lock_held = _acquire_local_lock(sync_key)
        if not (persistent_lock_held or local_lock_held):
            result.update(
                {
                    "status": "already_running",
                    "finished_at": _iso(_utc_now()),
                    "duration_seconds": 0.0,
                    "run_id": None,
                    "error": "already_running",
                }
            )
            return result
        result["run_id"] = run_id
        if persist_state:
            _persist_status(db, result, "running", sync_key)
            ensure_history_indexes(history_coll)

        existing = _existing_code_map(
            coll, office_id if len(office_ids) == 1 else None
        )
        result["mongo_total_before"] = len(existing)
        result["mongo_active_before"] = sum(
            bool(doc.get("disponible_prop360", True)) for doc in existing.values()
        )

        if prop360_client is None:
            email = os.getenv("PROP360_EMAIL")
            password = os.getenv("PROP360_PASSWORD")
            factory = client_factory or Prop360Client
            prop360_client = factory(email, password, delay=0.3)
        if not prop360_client.login():
            result["login"] = "ERROR"
            return _finish_result(
                db, result, "failed", started, run_id, sync_key, persist_state,
                error="login_failed",
            )
        result["login"] = "OK"

        scopes = []
        invalid_scope = None
        for current_office_id in office_ids:
            rows = prop360_client.fetch_listing(current_office_id)
            meta = dict(getattr(prop360_client, "last_listing_meta", {}) or {})
            scope_existing = existing
            if len(office_ids) > 1:
                scope_existing = {
                    code: doc
                    for code, doc in existing.items()
                    if (
                        doc.get("oficina_id") == current_office_id
                        or doc.get("oficina_nombre") == _office_name(current_office_id)
                        or (doc.get("resumen") or {}).get("oficina") == _office_name(current_office_id)
                    )
                }
            classified, validation_error = _classify_scope(
                current_office_id, rows, meta, scope_existing
            )
            safe_meta = {
                key: meta.get(key)
                for key in (
                    "office_id", "pages", "page_sizes", "rows",
                    "reported_total", "response_valid", "complete",
                )
            }
            if not classified.get("valid"):
                invalid_scope = {
                    "office_id": current_office_id,
                    "error": validation_error,
                }
                result["listing_meta"][_office_name(current_office_id)] = safe_meta
                break
            classified["listing_meta"] = safe_meta
            classified["existing"] = scope_existing
            scopes.append(classified)

        if invalid_scope:
            _add_error(result)
            return _finish_result(
                db, result, "failed", started, run_id, sync_key, persist_state,
                error=invalid_scope["error"], bajas_omitidas=True,
            )

        for scope in scopes:
            result["listing_meta"][scope["office"]] = scope["listing_meta"]
            result["prop360_total"] += len(scope["rows"])
            result["prop360_active"] += len(scope["active"])
            result["nuevas"] += len(scope["new_codes"])
            result["cambios_precio"] += len(scope["price_only"])
            result["cambios_ejecutivo"] += len(scope["executive_only"])
            result["cambios_precio_ejecutivo"] += len(scope["price_and_executive"])
            result["precios_ambiguos"] += len(scope["ambiguous_prices"])
            result["sin_cambios_operativos"] += len(scope["unchanged"])
            result["reactivaciones"] += len(scope["reactivations"])
            result["posibles_bajas"] += len(scope["possible_bajas"])
            result["possible_baja_codes"].extend(
                f"{scope['office_id']}:{code}" for code in scope["possible_bajas"]
            )
            result["operational_change_samples"].extend(scope["samples"])
            result["precio_ambiguo_codes"].extend(scope["ambiguous_prices"])

        result["fichas_completas_requeridas"] = result["nuevas"]
        result["procesadas"] = (
            result["nuevas"]
            + result["cambios_precio"]
            + result["cambios_ejecutivo"]
            + result["cambios_precio_ejecutivo"]
            + result["reactivaciones"]
        )
        result["modificadas"] = (
            result["cambios_precio"]
            + result["cambios_ejecutivo"]
            + result["cambios_precio_ejecutivo"]
        )
        result["sin_cambios"] = result["sin_cambios_operativos"]
        result["fichas_requeridas"] = result["fichas_completas_requeridas"]

        if persist_state:
            _persist_status(db, result, "running", sync_key, classified_at=_iso(_utc_now()))

        for scope in scopes:
            for code in scope["new_codes"]:
                if dry_run:
                    # Classification-only prevalidation must not open any
                    # detail page.  The count above is the number required by
                    # a subsequent real run.
                    continue
                result["fichas_completas_consultadas"] += 1
                try:
                    doc = scrape_propiedad(prop360_client, code, scope["active"][code])
                    if isinstance(doc.get("resumen"), dict):
                        listed = _parse_publication_price(scope["active"][code].get("precio"))
                        if listed:
                            doc["resumen"]["moneda_publicacion"] = listed["moneda"]
                            doc["resumen"]["monto_publicacion"] = listed["monto"]
                    doc["oficina_id"] = scope["office_id"]
                    doc["oficina_nombre"] = scope["office"]
                    if isinstance(doc.get("resumen"), dict):
                        doc["resumen"]["oficina"] = scope["office"]
                    if isinstance(doc.get("estado"), dict):
                        doc["estado"]["oficina"] = scope["office"]
                    if dry_run:
                        result["actualizadas"] += 1
                    else:
                        nuevo, actualizado = upsert_ficha(
                            coll,
                            doc,
                            history_coll=history_coll,
                            source=HISTORY_SOURCE,
                            sync_run_id=run_id,
                        )
                        if nuevo or actualizado:
                            result["actualizadas"] += 1
                except Exception:
                    _add_error(result, code)
                    log.exception("[PORTFOLIO_OPERATIONAL] fallo ficha %s", code)

            for update in scope["updates"]:
                code = update["codigo"]
                detected_at = _iso(_utc_now())
                set_ops, events = _operational_update(
                    scope["existing"][code],
                    scope["active"][code],
                    update["price_change"],
                    update["executive_change"],
                    update["reactivation"],
                    scope["office_id"],
                    run_id,
                    detected_at,
                )
                if not set_ops:
                    continue
                if not dry_run:
                    _write_events_and_update(
                        coll,
                        history_coll,
                        events,
                        {**_office_query(scope["office_id"]), "codigo": code},
                        set_ops,
                    )
                    result["actualizadas"] += 1

        if apply_bajas and not dry_run and result["errores"] == 0:
            for scope in scopes:
                for code in scope["possible_bajas"]:
                    existing_doc = scope["existing"][code]
                    detected_at = _iso(_utc_now())
                    set_ops, event = _baja_update(
                        existing_doc, scope["office_id"], run_id, detected_at
                    )
                    _write_events_and_update(
                        coll,
                        history_coll,
                        [event],
                        {**_office_query(scope["office_id"]), "codigo": code},
                        set_ops,
                    )
                    result["bajas_aplicadas"] += 1
        elif result["posibles_bajas"]:
            result["bajas_omitidas"] = True

        final_status = "failed" if result["errores"] else "completed"
        return _finish_result(
            db,
            result,
            final_status,
            started,
            run_id,
            sync_key,
            persist_state,
            bajas_omitidas=bool(result["bajas_omitidas"]),
        )
    except Exception as exc:
        log.exception("[PORTFOLIO_OPERATIONAL] ciclo fallido")
        _add_error(result)
        if persist_state and db is not None and persistent_lock_held:
            return _finish_result(
                db, result, "failed", started, run_id, sync_key, persist_state,
                error=type(exc).__name__,
            )
        result.update(
            {
                "status": "failed",
                "finished_at": _iso(_utc_now()),
                "duration_seconds": round((_utc_now() - started).total_seconds(), 3),
                "error": type(exc).__name__,
                "errors": result["errores"],
            }
        )
        return result
    finally:
        if local_lock_held:
            _release_local_lock(sync_key)
        if owned_prop360_client and prop360_client is not None:
            _close_prop360_client(prop360_client)
        if mongo_client is not None:
            mongo_client.close()


def run_sucre_portfolio_sync(
    *,
    dry_run: bool = False,
    apply_bajas: bool = False,
    db=None,
    prop360_client=None,
    client_factory: Callable[..., Any] | None = None,
    persist_state: bool | None = None,
) -> dict:
    """Compatibility entry point restricted to PROCASA SUCRE."""
    return run_portfolio_operational_sync(
        office_id=SUCRE_OFFICE_ID,
        dry_run=dry_run,
        apply_bajas=apply_bajas,
        db=db,
        prop360_client=prop360_client,
        client_factory=client_factory,
        persist_state=persist_state,
    )


__all__ = [
    "LISTING_CONTROL_FIELDS",
    "OPERATIONAL_LISTING_FIELDS",
    "SUCRE_OFFICE_ID",
    "SUCRE_OFFICE_NAME",
    "is_admin_user",
    "listing_fingerprint",
    "run_portfolio_operational_sync",
    "run_sucre_portfolio_sync",
]
