from copy import deepcopy
from datetime import datetime, timezone

from bson import ObjectId

from owner_scoring import calculate_owner_score, propose_classification_state
from recalculate_owner_score import build_plan, rollback_operations


def _doc():
    return {
        "_id": ObjectId(),
        "listing_id": "test-1",
        "origen": "yapo",
        "classification": {"state": "INCIERTO", "confidence": 0.82},
        "gestion": {"ejecutivo_asignado": "Paula", "ejecutivo_id": "u1"},
    }


def test_plan_is_idempotent_and_preserves_technical_confidence():
    doc = _doc()
    data = {"seller_type": "PARTICULAR", "publicador_visible": "Ana Perez"}
    result = calculate_owner_score(data)
    when = datetime(2026, 7, 14, tzinfo=timezone.utc)
    first = build_plan(doc, data, result, propose_classification_state(result), when)
    migrated = {**doc, **deepcopy(first["set"])}
    second = build_plan(
        migrated, data, result, propose_classification_state(result),
        datetime(2026, 7, 15, tzinfo=timezone.utc),
    )
    assert first["set"] == second["set"]
    assert second["set"]["classification"]["confidence"] == 0.82


def test_explicit_broker_is_removed_with_audit_trail():
    doc = _doc()
    data = {"company_name": "Grecop Corredores", "seller_type": "AGENTE"}
    result = calculate_owner_score(data)
    plan = build_plan(
        doc, data, result, propose_classification_state(result),
        datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    assert plan["proposed_state"] == "CORREDOR_SEGURO"
    assert plan["remove_assignment"] is True
    assert plan["set"]["gestion"]["ejecutivo_asignado"] is None
    audit = plan["set"]["gestion"]["historial_retiros_clasificacion"][0]
    assert audit["previous_assignment"]["ejecutivo_asignado"] == "Paula"


def test_rollback_replaces_full_classification_and_gestion_with_set_only():
    doc = _doc()
    payload = {"documents": [deepcopy(doc)]}
    operation = rollback_operations(payload)[0]
    assert operation._doc == {"$set": {
        "classification": doc["classification"],
        "gestion": doc["gestion"],
        "source_signals": None,
        "owner_score": None,
        "owner_score_version": None,
        "owner_score_signals": None,
    }}
