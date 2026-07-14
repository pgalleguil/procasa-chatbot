"""Dry-run/apply the six scope-audited Paula corrections only."""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from bson import json_util
from pymongo import MongoClient, UpdateOne

from config import Config


YAPO_IDS = ("32387250", "32387259", "32391134", "32400056")
TOCTOC_IDS = ("4092056", "4248794")
ALL_IDS = YAPO_IDS + TOCTOC_IDS
PROFILE_ID = "17631803"
RULE_VERSION = "scope-audit-paula-20260714-v1"


def _state(classification):
    value = str((classification or {}).get("state") or "")
    return "DUEÑO_SEGURO" if value.startswith("DUE") and value.endswith("O_SEGURO") else value


def profile_evidence(collection):
    docs = list(collection.find({"run_id": "yapo_paula_20260714", "seller_profile_id": PROFILE_ID}))
    brokers = [doc for doc in docs if _state(doc.get("classification")) == "CORREDOR_SEGURO"]
    brands = sorted({
        str(value).strip()
        for doc in brokers
        for value in (doc.get("company_name"), doc.get("broker_brand"), doc.get("publicador"))
        if value and any(term in str(value).lower() for term in (
            "propiedades", "corredor", "grecop", "inversiones", "ltda", "spa"
        ))
    })
    return {
        "seller_profile_id": PROFILE_ID,
        "linked_publications": len(docs),
        "confirmed_broker_publications": len(brokers),
        "commercial_names": brands,
        "run_id": "yapo_paula_20260714",
        "evidence_source": "same_profile_commercial_composition",
    }


def build_plans(collection, now):
    docs = list(collection.find({"listing_id": {"$in": list(ALL_IDS)}}))
    if len(docs) != 6:
        raise RuntimeError(f"Safety stop: expected 6 documents, found {len(docs)}")
    by_id = {str(doc["listing_id"]): doc for doc in docs}
    evidence = profile_evidence(collection)
    if evidence["linked_publications"] != 23 or evidence["confirmed_broker_publications"] < 19:
        raise RuntimeError(f"Profile evidence changed unexpectedly: {evidence}")
    plans = []
    for listing_id in ALL_IDS:
        doc = by_id[listing_id]
        old_classification = deepcopy(doc.get("classification") or {})
        old_state = _state(old_classification)
        new_classification = deepcopy(old_classification)
        new_gestion = deepcopy(doc.get("gestion") or {})
        if listing_id in YAPO_IDS:
            if doc.get("origen") != "yapo" or old_state != "INCIERTO":
                raise RuntimeError(f"Unexpected Yapo state for {listing_id}: {doc.get('origen')} {old_state}")
            if str(doc.get("seller_profile_id")) != PROFILE_ID:
                raise RuntimeError(f"Unexpected seller_profile_id for {listing_id}")
            reason = (
                f"seller_profile_id={PROFILE_ID} vincula {evidence['linked_publications']} publicaciones del run; "
                f"{evidence['confirmed_broker_publications']} ya están confirmadas como CORREDOR_SEGURO. "
                "El mismo perfil utiliza múltiples identidades comerciales verificadas, incluyendo "
                + ", ".join(evidence["commercial_names"])
                + ". La composición comercial del perfil, no el conteo por sí solo, confirma intermediación."
            )
            new_classification.update({
                "previous_classification_state": old_state,
                "state": "CORREDOR_SEGURO",
                "final_state": "CORREDOR_SEGURO",
                "confidence": 1.0,
                "score": 1.0,
                "reason": reason,
                "evidence": [
                    f"seller_profile_id={PROFILE_ID}",
                    f"linked_publications={evidence['linked_publications']}",
                    f"confirmed_broker_publications={evidence['confirmed_broker_publications']}",
                    "commercial_names=" + " | ".join(evidence["commercial_names"]),
                ],
                "decision_source": "profile_commercial_correlation",
                "version": RULE_VERSION,
                "classification_rule_version": RULE_VERSION,
                "profile_correlation": evidence,
                "semantic_check": {"status": "BROKER_EXCLUDED"},
                "updated_at": now,
            })
            previous_assignment = {
                key: new_gestion.get(key) for key in (
                    "ejecutivo_id", "ejecutivo_nombre", "ejecutivo_email", "ejecutivo_asignado",
                    "fecha_asignacion", "asignacion_version", "asignacion_comuna_slug",
                )
            }
            removal_history = list(new_gestion.get("historial_desasignaciones") or [])
            removal_history.append({
                "at": now,
                "reason": reason,
                "previous_assignment": previous_assignment,
                "classification_before": old_state,
                "classification_after": "CORREDOR_SEGURO",
                "rule_version": RULE_VERSION,
            })
            new_gestion.update({
                "ejecutivo_id": None,
                "ejecutivo_nombre": None,
                "ejecutivo_email": None,
                "ejecutivo_asignado": None,
                "fecha_asignacion": None,
                "historial_desasignaciones": removal_history,
                "semantic_review_hold": True,
                "excluir_asignacion": True,
                "excluir_asignacion_reason": "CORREDOR_SEGURO_PROFILE_COMMERCIAL_CORRELATION",
            })
        else:
            if doc.get("origen") != "toctoc" or old_state != "DUEÑO_SEGURO":
                raise RuntimeError(f"Unexpected TocToc state for {listing_id}: {doc.get('origen')} {old_state}")
            if listing_id == "4092056":
                confidence = 0.5
                reason = (
                    "La frase 'trato directo con sus dueños' está en tercera persona: distingue al anunciante "
                    "de los propietarios y no acredita que quien publica sea dueño."
                )
                evidence_list = ["third_person_reference=trato directo con sus dueños"]
            else:
                confidence = 0.6
                reason = (
                    "La frase 'vendida directamente por sus dueños' es una señal favorable débil, pero está "
                    "en tercera persona y no identifica al anunciante como propietario. Requiere confirmación manual."
                )
                evidence_list = ["weak_owner_context=vendida directamente por sus dueños"]
            new_classification.update({
                "previous_classification_state": old_state,
                "state": "INCIERTO",
                "final_state": "INCIERTO",
                "confidence": confidence,
                "score": confidence,
                "reason": reason,
                "evidence": evidence_list,
                "decision_source": "manual_scope_audit",
                "version": RULE_VERSION,
                "classification_rule_version": RULE_VERSION,
                "semantic_check": {"status": "VALID_MANUAL_REVIEW"},
                "manual_review": {
                    "status": "APPROVED_INCIERTO",
                    "reviewed_at": now,
                    "reason": reason,
                },
                "updated_at": now,
            })
        plans.append({
            "_id": doc["_id"],
            "listing_id": listing_id,
            "origin": doc.get("origen"),
            "before": {"state": old_state, "confidence": old_classification.get("confidence")},
            "after": {"state": new_classification["state"], "confidence": new_classification["confidence"]},
            "evidence": new_classification["evidence"],
            "remove_from_paula": listing_id in YAPO_IDS,
            "set": {"classification": new_classification, "gestion": new_gestion},
        })
    return plans


def write_backup(docs, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_util.dumps({
        "format": "paula-six-pre-correction-v1",
        "created_at": datetime.now(timezone.utc),
        "count": len(docs),
        "listing_ids": list(ALL_IDS),
        "documents": docs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--report", type=Path, default=Path("reports/paula_six_corrections_dry_run.json"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-backup", type=Path)
    args = parser.parse_args()
    collection = MongoClient(Config.MONGO_URI)[Config.DB_NAME][Config.CAPTACION_COLLECTION_NAME]
    docs = list(collection.find({"listing_id": {"$in": list(ALL_IDS)}}))
    if len(docs) != 6:
        raise RuntimeError("Safety stop: exact six-document cohort not found")
    if args.backup:
        write_backup(docs, args.backup)
    now = datetime.now(timezone.utc)
    plans = build_plans(collection, now)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "mode": "APPLY" if args.apply else "DRY_RUN_READ_ONLY",
        "count": len(plans),
        "plans": [{key: value for key, value in plan.items() if key not in {"_id", "set"}} for plan in plans],
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if args.apply:
        if not args.approved_backup or not args.approved_backup.is_file():
            raise RuntimeError("--apply requires --approved-backup")
        backup = json_util.loads(args.approved_backup.read_text(encoding="utf-8"))
        if backup.get("count") != 6 or set(backup.get("listing_ids") or []) != set(ALL_IDS):
            raise RuntimeError("Approved backup does not match six-document cohort")
        result = collection.bulk_write([
            UpdateOne({"_id": plan["_id"]}, {"$set": plan["set"]}) for plan in plans
        ], ordered=True)
        print(json.dumps({"matched": result.matched_count, "modified": result.modified_count}))
    else:
        print(json.dumps({"mode": "DRY_RUN_READ_ONLY", "count": len(plans)}, indent=2))


if __name__ == "__main__":
    main()
