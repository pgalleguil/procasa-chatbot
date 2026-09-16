"""Audit and safely clean current Toctoc assignments held by Hernan.

The default mode is read-only.  ``--apply`` removes only assignments for
which the stored Mongo identity or the local Toctoc HTML proves a commercial
publisher.  Raw evidence is used only as a hard publisher/profile signal;
description text is never used to make this cleanup decision.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from broker_identity import detect_hard_broker_signal  # noqa: E402
from config import Config  # noqa: E402


HERNAN_ID = "6a681413140190dde11f26d1"
TOCTOC_ORIGIN = "toctoc"
HTML_ROOT = ROOT / "scrapers" / "scraper_toctoc" / "html_dumps"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _state(doc: dict[str, Any]) -> str:
    return str((doc.get("classification") or {}).get("state") or "").strip().upper()


def _listing_code(doc: dict[str, Any]) -> str:
    return str(doc.get("listing_id") or doc.get("codigo") or doc.get("_id") or "")


def _publisher(doc: dict[str, Any]) -> str:
    return str(
        doc.get("publicador_visible")
        or doc.get("seller_name")
        or doc.get("contact_name")
        or ""
    ).strip()


def _metadata_paths() -> dict[str, Path]:
    if not HTML_ROOT.exists():
        return {}
    return {path.name: path for path in HTML_ROOT.rglob("*.json")}


def _raw_profile(url: str, metadata_paths: dict[str, Path]) -> dict[str, Any] | None:
    if not url:
        return None
    candidates = (
        f"{hashlib.md5(url.encode('utf-8')).hexdigest()}.json",
        f"{hashlib.md5(url.split('?', 1)[0].encode('utf-8')).hexdigest()}.json",
    )
    metadata_path = next((metadata_paths[name] for name in candidates if name in metadata_paths), None)
    if metadata_path is None:
        return None
    html_path = metadata_path.with_suffix(".html")
    if not html_path.exists():
        return {"metadata_path": str(metadata_path), "html_available": False}
    source = html_path.read_text(encoding="utf-8", errors="replace")
    client_match = re.search(
        r'"client"\s*:\s*\{\s*"id"\s*:\s*(\d+)\s*,\s*'
        r'"name"\s*:\s*"(.*?)"\s*,\s*"logo"\s*:\s*"(.*?)"',
        source,
    )
    operation_match = re.search(
        r'"operation"\s*:\s*\{\s*"operation"\s*:\s*"(.*?)"',
        source,
    )
    client_name = html.unescape(client_match.group(2)) if client_match else ""
    client_logo = html.unescape(client_match.group(3)) if client_match else ""
    operation = html.unescape(operation_match.group(1)) if operation_match else ""
    raw_broker = "/corredora/" in client_logo.lower() or bool(
        re.search(r"\bcorredor(?:a|es)?\b", operation, re.IGNORECASE)
    )
    return {
        "metadata_path": str(metadata_path),
        "html_available": True,
        "client_id": client_match.group(1) if client_match else "",
        "client_name": client_name,
        "client_logo": client_logo,
        "operation": operation,
        "commercial_profile": raw_broker,
    }


def _projection() -> dict[str, int]:
    return {
        "_id": 1,
        "origen": 1,
        "listing_id": 1,
        "codigo": 1,
        "url": 1,
        "canonical_url": 1,
        "comuna": 1,
        "title": 1,
        "titulo": 1,
        "description": 1,
        "descripcion": 1,
        "publicador_visible": 1,
        "seller_name": 1,
        "seller_type": 1,
        "seller_type_source": 1,
        "seller_type_evidence": 1,
        "seller_profile_id": 1,
        "seller_profile_url": 1,
        "seller_url": 1,
        "classification": 1,
        "gestion": 1,
        "source_signals": 1,
        "html_path": 1,
        "html_sha256": 1,
    }


def audit(db) -> dict[str, Any]:
    collection = db[Config.CAPTACION_COLLECTION_NAME]
    cursor = collection.find(
        {"origen": TOCTOC_ORIGIN, "gestion.ejecutivo_id": HERNAN_ID},
        _projection(),
    ).batch_size(40).max_time_ms(180000)
    docs = list(cursor)
    metadata_paths = _metadata_paths()
    rows: list[dict[str, Any]] = []
    hard_assigned: list[dict[str, Any]] = []
    raw_missing = 0
    raw_available = 0
    raw_commercial = 0
    mongo_publisher_counts = Counter()
    raw_publisher_counts = Counter()

    for doc in docs:
        cls = doc.get("classification") or {}
        url = str(doc.get("canonical_url") or doc.get("url") or "")
        mongo_publisher = _publisher(doc)
        mongo_signal = detect_hard_broker_signal(doc, extracted=doc)
        raw = _raw_profile(url, metadata_paths)
        if not raw or not raw.get("html_available"):
            raw_missing += 1
        else:
            raw_available += 1
        raw_is_commercial = bool(raw and raw.get("commercial_profile"))
        if raw_is_commercial:
            raw_commercial += 1
            raw_publisher_counts[str(raw.get("client_name") or mongo_publisher)] += 1
        mongo_publisher_counts[mongo_publisher or "[EMPTY]"] += 1

        effective_signal = mongo_signal
        if raw_is_commercial:
            effective_signal = {
                "source_field": "raw_next_data.client/operation",
                "value": raw.get("client_name") or mongo_publisher,
                "reason_code": "TOCTOC_RAW_CORREDORA_PROFILE",
                "evidence": (
                    f"client={raw.get('client_name') or mongo_publisher}; "
                    f"client_id={raw.get('client_id') or 'unknown'}; "
                    f"logo={raw.get('client_logo') or 'missing'}; "
                    f"operation={raw.get('operation') or 'missing'}"
                ),
            }
        if effective_signal:
            row = {
                "mongo_id": doc.get("_id"),
                "codigo": _listing_code(doc),
                "comuna": str(doc.get("comuna") or ""),
                "url": url,
                "publicador_visible": mongo_publisher,
                "raw_publicador": str(raw.get("client_name") or "") if raw else "",
                "seller_type": str(doc.get("seller_type") or ""),
                "classification_previa": _state(doc),
                "rule_state": str(cls.get("rule_state") or ""),
                "hard_broker_signal": True,
                "motivo": effective_signal["reason_code"],
                "evidence": effective_signal["evidence"],
                "raw_available": bool(raw and raw.get("html_available")),
                "raw_operation": str(raw.get("operation") or "") if raw else "",
                "assigned_executive": "Hernán Castro",
                "action": "REMOVE_ASSIGNMENT_AND_MARK_CORREDOR",
            }
            rows.append(row)
            hard_assigned.append(row)

    return {
        "hernan_toctoc_current": len(docs),
        "hernan_reaudited": len(docs),
        "hard_brokers_still_assigned": len(hard_assigned),
        "commercial_publishers_not_recognized": sum(
            1 for row in hard_assigned if row["motivo"] == "TOCTOC_RAW_CORREDORA_PROFILE"
        ),
        "raw_available": raw_available,
        "raw_missing": raw_missing,
        "raw_commercial": raw_commercial,
        "mongo_publisher_breakdown": dict(mongo_publisher_counts.most_common()),
        "raw_publisher_breakdown": dict(raw_publisher_counts.most_common()),
        "cases": rows,
    }


def _classification_after(doc: dict[str, Any], signal: dict[str, Any], now: datetime) -> dict[str, Any]:
    classification = copy.deepcopy(doc.get("classification") or {})
    previous_state = _state(doc)
    classification.update({
        "state": "CORREDOR_SEGURO",
        "final_state": "CORREDOR_SEGURO",
        "hard_broker_signal": True,
        "hard_veto": "PROFESSIONAL",
        "professional_hard_veto": True,
        "assignment_ready": False,
        "exclude_from_assignment": True,
        "hard_broker_signal_source_field": signal["source_field"],
        "hard_broker_signal_reason": signal["reason_code"],
        "hard_broker_signal_evidence": [signal["evidence"]],
        "reclassified_by": "hard_publisher_broker_veto",
        "reclassified_at": now,
        "reclassification_reason": "publicador_visible_comercial_explicito",
        "assignment_block_reasons": ["HARD_BROKER_PUBLISHER_VETO"],
    })
    history = list(classification.get("reclassification_history") or [])
    history.append({
        "event": "hard_publisher_broker_veto",
        "at": now,
        "portal": TOCTOC_ORIGIN,
        "listing_id": _listing_code(doc),
        "previous_state": previous_state,
        "new_state": "CORREDOR_SEGURO",
        "source_field": signal["source_field"],
        "reason_code": signal["reason_code"],
        "evidence": signal["evidence"],
        "assignment_removed": True,
        "assignment_version": (doc.get("gestion") or {}).get("asignacion_version"),
    })
    classification["reclassification_history"] = history[-20:]
    return classification


def apply(db, report: dict[str, Any]) -> dict[str, int]:
    collection = db[Config.CAPTACION_COLLECTION_NAME]
    now = utcnow()
    metadata_paths = _metadata_paths()
    removed = 0
    skipped = 0
    failed = 0
    for row in report["cases"]:
        doc = collection.find_one({
            "_id": row["mongo_id"],
            "origen": TOCTOC_ORIGIN,
            "gestion.ejecutivo_id": HERNAN_ID,
        }, _projection())
        if not doc:
            skipped += 1
            continue
        raw = _raw_profile(
            str(doc.get("canonical_url") or doc.get("url") or ""),
            metadata_paths,
        )
        mongo_signal = detect_hard_broker_signal(doc, extracted=doc)
        raw_is_commercial = bool(raw and raw.get("commercial_profile"))
        signal = mongo_signal
        if raw_is_commercial:
            signal = {
                "source_field": "raw_next_data.client/operation",
                "value": raw.get("client_name") or _publisher(doc),
                "reason_code": "TOCTOC_RAW_CORREDORA_PROFILE",
                "evidence": (
                    f"client={raw.get('client_name') or _publisher(doc)}; "
                    f"client_id={raw.get('client_id') or 'unknown'}; "
                    f"logo={raw.get('client_logo') or 'missing'}; "
                    f"operation={raw.get('operation') or 'missing'}"
                ),
            }
        if not signal:
            skipped += 1
            continue
        classification = _classification_after(doc, signal, now)
        try:
            result = collection.update_one(
                {
                    "_id": doc["_id"],
                    "origen": TOCTOC_ORIGIN,
                    "gestion.ejecutivo_id": HERNAN_ID,
                },
                {"$set": {
                    "classification": classification,
                    "gestion.estado": "Corredor",
                    "gestion.ejecutivo_id": None,
                    "gestion.ejecutivo": None,
                    "gestion.ejecutivo_asignado": None,
                    "gestion.ejecutivo_nombre": None,
                    "gestion.ejecutivo_email": None,
                    "gestion.desasignada_por": "hard_publisher_broker_veto",
                    "gestion.fecha_desasignacion": now,
                    "gestion.desasignacion_motivo": "publicador_visible_comercial_explicito",
                    "updated_at": now,
                }},
            )
            if result.modified_count == 1:
                removed += 1
            else:
                skipped += 1
        except Exception:
            failed += 1
    return {"brokers_removed_now": removed, "skipped": skipped, "failed": failed}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    client = MongoClient(
        Config.MONGO_URI,
        socketTimeoutMS=60000,
        connectTimeoutMS=10000,
        serverSelectionTimeoutMS=20000,
        retryReads=True,
    )
    try:
        db = client[Config.DB_NAME]
        report = audit(db)
        if args.apply:
            report["apply"] = apply(db, report)
            after = audit(db)
            report["after"] = {
                key: after[key]
                for key in ("hernan_toctoc_current", "hard_brokers_still_assigned", "raw_commercial")
            }
        if args.output:
            Path(args.output).write_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        summary = {key: value for key, value in report.items() if key != "cases"}
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    finally:
        client.close()


if __name__ == "__main__":
    main()
