"""Finalize and assign only the controlled Paula discovery cohort."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from bson import json_util
from pymongo import MongoClient

from config import Config
from captacion_assignment_eligibility import assignment_eligibility

RUN_RE = "^toctoc_scrape_20260714_18"
THIRD_PERSON_IDS = {"4181850", "4244544", "4244553", "4244565"}


def main() -> None:
    db = MongoClient(Config.MONGO_URI)[Config.DB_NAME]
    coll = db[Config.CAPTACION_COLLECTION_NAME]
    paula = db.usuarios.find_one({"nombre": "Paula Morales"})
    docs = list(coll.find({"origen": "toctoc", "run_id": {"$regex": RUN_RE}}))
    if len(docs) != 8 or not paula:
        raise RuntimeError(f"Safety stop: expected Paula and 8 controlled docs, found {len(docs)}")

    backup = Path("backups/paula_discovery_toctoc_pre_finalize_20260714.json")
    backup.write_text(json_util.dumps({"count": len(docs), "documents": docs}, ensure_ascii=False, indent=2), encoding="utf-8")
    now = datetime.now(timezone.utc)

    for doc in docs:
        lid = str(doc["listing_id"])
        if lid not in THIRD_PERSON_IDS:
            continue
        cls = deepcopy(doc.get("classification") or {})
        cls.update({
            "previous_classification_state": cls.get("state"),
            "state": "INCIERTO", "final_state": "INCIERTO", "confidence": 0.6, "score": 0.6,
            "reason": "Validación posterior de corrida: 'Vende dueño'/'Dueña arrienda' está en tercera persona y no identifica inequívocamente al publicador.",
            "post_validation": "third_person_owner_phrase_downgraded",
            "deepseek_proposed_state": cls.get("state"),
            "assignment_ready": True,
            "assignment_gate_version": "assignment-gate-v2",
            "classification_rule_version": "toctoc-owner-rules-v2",
        })
        coll.update_one({"_id": doc["_id"], "run_id": doc["run_id"]}, {"$set": {"classification": cls}})

    docs = list(coll.find({"origen": "toctoc", "run_id": {"$regex": RUN_RE}}))
    eligible = []
    blocked = []
    for doc in docs:
        ok, reasons = assignment_eligibility(doc)
        (eligible if ok else blocked).append((doc, reasons))

    assigned = []
    for doc, _ in eligible:
        result = coll.update_one(
            {"_id": doc["_id"], "classification.assignment_ready": True,
             "$or": [{"gestion.ejecutivo_id": {"$exists": False}}, {"gestion.ejecutivo_id": None}]},
            {"$set": {
                "gestion.ejecutivo_id": str(paula["_id"]),
                "gestion.ejecutivo_nombre": paula["nombre"],
                "gestion.ejecutivo_email": paula.get("email", ""),
                "gestion.ejecutivo_asignado": paula["nombre"],
                "gestion.fecha_asignacion": now,
                "gestion.asignacion_version": "paula-controlled-discovery-20260714-v1",
                "gestion.classification_at_assignment": doc["classification"]["state"],
                "gestion.estado": "NUEVO",
            }, "$push": {"gestion.historial_asignaciones": {
                "ejecutivo_id": str(paula["_id"]), "ejecutivo_nombre": paula["nombre"],
                "classification_state": doc["classification"]["state"], "assigned_at": now,
                "assignment_version": "paula-controlled-discovery-20260714-v1",
            }}}
        )
        if result.modified_count:
            assigned.append(str(doc["listing_id"]))

    print(json_util.dumps({
        "cohort": len(docs), "eligible": len(eligible), "assigned": assigned,
        "blocked": [{"listing_id": str(doc["listing_id"]), "reasons": reasons} for doc, reasons in blocked],
        "paula_final": coll.count_documents({"gestion.ejecutivo_asignado": "Paula Morales"}),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
