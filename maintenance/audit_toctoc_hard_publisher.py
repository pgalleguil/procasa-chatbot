"""Audit all Toctoc listings and optionally apply the hard publisher veto.

The default mode is read-only.  ``--apply-hernan`` is intentionally narrow
and retained for the original Hernan-only cleanup.  ``--apply-global`` applies
the same deterministic veto to the remaining current Toctoc false negatives.
Both modes use an optimistic filter per document so retries are idempotent.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from broker_identity import detect_hard_broker_signal  # noqa: E402
from config import Config  # noqa: E402
from owner_probability import apply_owner_probability_to_document  # noqa: E402


HERNAN_ID = "6a681413140190dde11f26d1"
HERNAN_ASSIGNMENT_VERSION = "hernan_capacity_exception_v1"
APPROVED_HERNAN_PUBLISHERS = frozenset({
    "Kutt Property",
    "Grupo Premium",
    "Magnolia Property",
    "Property Partners",
    "PROPERTY PARTNERS",
    "Alejandro Jaime Realty Corp",
    "Century21 Conecta",
})
HERNAN_FILTER = {
    "origen": "toctoc",
    "gestion.ejecutivo_id": HERNAN_ID,
    "gestion.asignacion_version": HERNAN_ASSIGNMENT_VERSION,
}
PHONE_RAW_FIELDS = (
    "phone_original_value", "telefono", "telefono_original", "contact_phone",
    "whatsapp_phone", "phone", "phone_raw",
)
PHONE_NORMALIZED_FIELDS = (
    "phone_normalized", "telefono_normalizado", "phone_normalized_value",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], {}, ())


def _assigned_id(doc: dict[str, Any]) -> str:
    gestion = doc.get("gestion") or {}
    value = gestion.get("ejecutivo_id")
    return str(value).strip() if _nonempty(value) else ""


def _state(doc: dict[str, Any]) -> str:
    return str((doc.get("classification") or {}).get("state") or "").strip().upper()


def _classification_after(doc: dict[str, Any], signal: dict[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(doc)
    apply_owner_probability_to_document(candidate)
    cls = candidate.setdefault("classification", {})
    cls["hard_broker_signal"] = True
    cls["hard_veto"] = "PROFESSIONAL"
    cls["professional_hard_veto"] = True
    cls["hard_broker_signal_source_field"] = signal["source_field"]
    cls["hard_broker_signal_reason"] = signal["reason_code"]
    cls["hard_broker_signal_evidence"] = [signal["evidence"]]
    return cls


def _phone_presence(doc: dict[str, Any]) -> tuple[bool, bool]:
    raw = any(_nonempty(doc.get(key)) for key in PHONE_RAW_FIELDS)
    normalized = any(_nonempty(doc.get(key)) for key in PHONE_NORMALIZED_FIELDS)
    return raw, normalized


def _user_names(db) -> dict[str, str]:
    result: dict[str, str] = {}
    for user in db["usuarios"].find({}, {"_id": 1, "nombre": 1, "name": 1, "email": 1}):
        result[str(user.get("_id"))] = str(user.get("nombre") or user.get("name") or user.get("email") or user.get("_id"))
    return result


def _load_toctoc(db) -> list[dict[str, Any]]:
    projection = {
        "_id": 1, "origen": 1, "listing_id": 1, "url": 1, "canonical_url": 1,
        "comuna": 1, "classification.state": 1, "classification.source": 1,
        "classification.final_state": 1, "publicador_visible": 1,
        "seller_name": 1, "seller_type": 1, "seller_type_source": 1,
        "seller_type_evidence": 1, "seller_profile_id": 1, "seller_profile_url": 1,
        "contact_name": 1, "contact_logo_alt": 1, "seller_jsonld_name": 1,
        "listing_advertiser": 1, "company_name": 1, "broker_brand": 1,
        "gestion": 1, "phone_original_value": 1,
        "telefono": 1, "telefono_original": 1, "contact_phone": 1,
        "whatsapp_phone": 1, "phone": 1, "phone_raw": 1,
        "phone_normalized": 1, "telefono_normalizado": 1,
    }
    cursor = db[Config.CAPTACION_COLLECTION_NAME].find(
        {"origen": "toctoc"}, projection
    ).hint([("origen", 1), ("listing_id", 1)]).batch_size(100).max_time_ms(180000)
    return list(cursor)


def audit(db) -> dict[str, Any]:
    docs = _load_toctoc(db)
    users = _user_names(db)
    current_broker = 0
    new_brokers: list[dict[str, Any]] = []
    current_owner_overridden = 0
    current_uncertain_overridden = 0
    assigned_affected = 0
    executives = Counter()
    communes = Counter()
    publisher_counts = Counter()
    hernan_docs = [doc for doc in docs if HERNAN_FILTER.items() <= {
        ("origen", doc.get("origen")),
        ("gestion.ejecutivo_id", (doc.get("gestion") or {}).get("ejecutivo_id")),
        ("gestion.asignacion_version", (doc.get("gestion") or {}).get("asignacion_version")),
    }]
    phone_raw = phone_normalized = 0

    for doc in docs:
        before = _state(doc)
        if before.startswith("CORREDOR"):
            current_broker += 1
        signal = detect_hard_broker_signal(doc, extracted=doc)
        if not signal:
            continue
        publisher_counts[str(signal.get("value") or "")] += 1
        # A confirmed hard publisher signal is the final broker decision by
        # contract; no owner-probability/DeepSeek calculation is needed for a
        # read-only volume count.
        after_state = "CORREDOR_SEGURO"
        if not before.startswith("CORREDOR") and after_state.startswith("CORREDOR"):
            row = {
                "mongo_id": doc.get("_id"),
                "_id": str(doc.get("_id")),
                "listing_id": str(doc.get("listing_id") or ""),
                "comuna": str(doc.get("comuna") or ""),
                "url": str(doc.get("canonical_url") or doc.get("url") or ""),
                "publisher": str(signal.get("value") or ""),
                "current_state": before,
                "source_field": signal.get("source_field", ""),
                "reason_code": signal.get("reason_code", ""),
                "evidence": signal.get("evidence", ""),
                "executive_id": _assigned_id(doc),
                "executive": users.get(_assigned_id(doc), _assigned_id(doc)),
            }
            new_brokers.append(row)
            communes[row["comuna"]] += 1
            if row["executive_id"]:
                assigned_affected += 1
                executives[row["executive"]] += 1
            if before in {"DUEÑO_SEGURO", "DUEÑO_PROBABLE"}:
                current_owner_overridden += 1
            if before == "INCIERTO":
                current_uncertain_overridden += 1

    for doc in hernan_docs:
        raw, normalized = _phone_presence(doc)
        phone_raw += int(raw)
        phone_normalized += int(normalized)

    return {
        "total_toctoc_reprocessed": len(docs),
        "current_broker": current_broker,
        "new_brokers_detected": len(new_brokers),
        "current_owner_overridden": current_owner_overridden,
        "current_uncertain_overridden": current_uncertain_overridden,
        "assigned_properties_affected": assigned_affected,
        "executives_affected": len(executives),
        "executive_breakdown": dict(sorted(executives.items())),
        "commune_breakdown": dict(sorted(communes.items())),
        "publisher_breakdown": dict(publisher_counts.most_common()),
        "new_brokers": new_brokers,
        "hernan_audit_total": len(hernan_docs),
        "hernan_confirmed_rows": sum(
            row["publisher"] in APPROVED_HERNAN_PUBLISHERS
            for row in new_brokers
            if row["executive_id"] == HERNAN_ID
        ),
        "hernan_additional_publisher_term_rows_held": sum(
            row["publisher"] not in APPROVED_HERNAN_PUBLISHERS
            for row in new_brokers
            if row["executive_id"] == HERNAN_ID
        ),
        "hernan_phone_raw_available": phone_raw,
        "hernan_phone_normalized_available": phone_normalized,
        "hernan_phone_lost_at_stage": (
            "TOCTOC_EXTRACTOR: no phone field is emitted; crm_schema only consumes optional phone input"
            if phone_raw == 0 and phone_normalized == 0
            else "not all phone fields are available; inspect per-document provenance"
        ),
        "hernan_phone_learning_currently_effective": (
            "CRM/manual phone learning only; no scraper phone available in this batch"
            if phone_raw == 0 and phone_normalized == 0
            else "effective for available normalized phones"
        ),
    }


def apply_hernan(db, rows: list[dict[str, Any]]) -> dict[str, int]:
    collection = db[Config.CAPTACION_COLLECTION_NAME]
    now = utcnow()
    assigned_before = 0
    skipped = 0
    failed = 0

    for row in rows:
        if row.get("publisher") not in APPROVED_HERNAN_PUBLISHERS:
            skipped += 1
            continue
        doc = collection.find_one(
            {
                "_id": row["mongo_id"],
                **HERNAN_FILTER,
                "classification.state": {"$nin": ["CORREDOR_SEGURO", "CORREDOR_PROBABLE"]},
            }
        )
        if not doc:
            skipped += 1
            continue
        signal = detect_hard_broker_signal(doc, extracted=doc)
        if not signal:
            skipped += 1
            continue
        previous_state = _state(doc)
        classification = _classification_after(doc, signal)
        entry = {
            "event": "hard_publisher_broker_veto",
            "at": now,
            "portal": "toctoc",
            "listing_id": str(doc.get("listing_id") or ""),
            "previous_state": previous_state,
            "new_state": "CORREDOR_SEGURO",
            "source_field": signal["source_field"],
            "reason_code": signal["reason_code"],
            "evidence": signal["evidence"],
            "assignment_version": HERNAN_ASSIGNMENT_VERSION,
        }
        classification["reclassified_by"] = "hard_publisher_broker_veto"
        classification["reclassified_at"] = now
        classification["reclassification_reason"] = "publicador_visible_comercial_explicito"
        history = list(classification.get("reclassification_history") or [])
        history.append(entry)
        classification["reclassification_history"] = history[-20:]
        try:
            result = collection.update_one(
                {
                    "_id": doc["_id"],
                    **HERNAN_FILTER,
                    "classification.state": {"$nin": ["CORREDOR_SEGURO", "CORREDOR_PROBABLE"]},
                },
                {
                    "$set": {
                        "classification": classification,
                        "gestion.estado": "Corredor",
                        "gestion.ejecutivo_id": None,
                        "gestion.ejecutivo": None,
                        "gestion.desasignada_por": "hard_publisher_broker_veto",
                        "gestion.fecha_desasignacion": now,
                        "gestion.desasignacion_motivo": "publicador_visible_comercial_explicito",
                        "updated_at": now,
                    }
                },
            )
            if result.modified_count == 1:
                assigned_before += 1
            else:
                skipped += 1
        except Exception:
            failed += 1
    return {"hernan_confirmed_brokers_removed": assigned_before, "skipped": skipped, "failed": failed}


def apply_global(db, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Apply only currently detected hard-publisher false negatives in Toctoc.

    The portal filter is explicit and the state predicate makes the operation
    safe to retry.  No document without a freshly recomputed hard signal is
    written, and no Yapo or other portal document can match this operation.
    """
    collection = db[Config.CAPTACION_COLLECTION_NAME]
    now = utcnow()
    corrected = 0
    assigned_removed = 0
    skipped = 0
    failed = 0

    for row in rows:
        doc = collection.find_one(
            {
                "_id": row["mongo_id"],
                "origen": "toctoc",
                "classification.state": {"$nin": ["CORREDOR_SEGURO", "CORREDOR_PROBABLE"]},
            }
        )
        if not doc:
            skipped += 1
            continue
        signal = detect_hard_broker_signal(doc, extracted=doc)
        if not signal:
            skipped += 1
            continue
        previous_state = _state(doc)
        previous_executive_id = _assigned_id(doc)
        classification = _classification_after(doc, signal)
        entry = {
            "event": "hard_publisher_broker_veto",
            "at": now,
            "portal": "toctoc",
            "listing_id": str(doc.get("listing_id") or ""),
            "previous_state": previous_state,
            "new_state": "CORREDOR_SEGURO",
            "source_field": signal["source_field"],
            "reason_code": signal["reason_code"],
            "evidence": signal["evidence"],
            "previous_executive_id": previous_executive_id or None,
        }
        classification["reclassified_by"] = "hard_publisher_broker_veto"
        classification["reclassified_at"] = now
        classification["reclassification_reason"] = "publicador_visible_comercial_explicito"
        history = list(classification.get("reclassification_history") or [])
        history.append(entry)
        classification["reclassification_history"] = history[-20:]
        set_fields: dict[str, Any] = {
            "classification": classification,
            "updated_at": now,
        }
        if previous_executive_id:
            set_fields.update({
                "gestion.estado": "Corredor",
                "gestion.ejecutivo_id": None,
                "gestion.ejecutivo": None,
                "gestion.desasignada_por": "hard_publisher_broker_veto",
                "gestion.fecha_desasignacion": now,
                "gestion.desasignacion_motivo": "publicador_visible_comercial_explicito",
            })
        try:
            result = collection.update_one(
                {
                    "_id": doc["_id"],
                    "origen": "toctoc",
                    "classification.state": {"$nin": ["CORREDOR_SEGURO", "CORREDOR_PROBABLE"]},
                },
                {"$set": set_fields},
            )
            if result.modified_count == 1:
                corrected += 1
                assigned_removed += int(bool(previous_executive_id))
            else:
                skipped += 1
        except Exception:
            failed += 1
    return {
        "additional_corrected": corrected,
        "assigned_properties_removed": assigned_removed,
        "skipped": skipped,
        "failed": failed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-hernan", action="store_true")
    parser.add_argument("--apply-global", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    # Use the project's already-configured URI, with a longer read timeout for
    # this one-off audit.  No alternate database or credentials are created.
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
        if args.apply_hernan and args.apply_global:
            parser.error("use only one apply mode")
        if args.apply_hernan:
            result = apply_hernan(db, report["new_brokers"])
            report["hernan_apply"] = result
            # Re-read after the narrow write to verify the final state and count.
            report["hernan_remaining_assignments"] = db[Config.CAPTACION_COLLECTION_NAME].count_documents(HERNAN_FILTER)
        if args.apply_global:
            result = apply_global(db, report["new_brokers"])
            report["global_apply"] = result
            report["remaining_hard_publisher_false_negatives"] = audit(db)["new_brokers_detected"]
        if args.output:
            Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        summary = {key: value for key, value in report.items() if key not in {"new_brokers", "publisher_breakdown"}}
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        if args.apply_hernan:
            print(json.dumps({"apply": report.get("hernan_apply", {})}, ensure_ascii=False, indent=2, default=str))
        if args.apply_global:
            print(json.dumps({"apply": report.get("global_apply", {})}, ensure_ascii=False, indent=2, default=str))
    finally:
        client.close()


if __name__ == "__main__":
    main()
