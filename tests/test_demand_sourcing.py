from datetime import datetime, timezone

import mongomock
from bson import ObjectId

from chatbot.demand_sourcing import (
    find_demand_captation_candidates, ensure_admin_demand_review,
)

NOW = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)


def search():
    return {
        "status": "ACTIVE", "followup_required": True, "matches": [],
        "criteria": {
            "operation": "VENTA", "max_price_clp": 97_000_000,
            "comunas": ["Maipú", "Pudahuel"],
            "property_types": ["CASA", "DEPARTAMENTO"],
            "preferred_property_type": "CASA",
        },
    }


def listing(id, *, price=90_000_000, kind="casa", comuna="Maipu",
            klass="INCIERTO", mgmt=None, description="", address=""):
    return {
        "listing_id": str(id), "source_portal": "toctoc",
        "url": f"https://www.toctoc.com/propiedades/{id}",
        "tipo_propiedad": kind, "operacion": "venta", "comuna": comuna,
        "precio_clp_normalizado": price, "precio_validacion": "OK",
        "classification": {"state": klass, "final_state": klass},
        "gestion": mgmt or {}, "description": description,
        "direccion_exacta": address,
    }


def test_diana_sourcing_prefers_unassigned_houses_and_uses_history():
    db = mongomock.MongoClient().sourcing
    docs = [
        listing("good", price=95_000_000, kind="casa"),
        listing("apartment", price=70_000_000, kind="departamento", comuna="Pudahuel"),
        listing("broker", klass="INCIERTO", mgmt={"estado_captacion": "Corredor"}),
        listing("broker-ai", klass="CORREDOR_SEGURO"),
        listing("no-broker", description="Venta con dueño. Corredoras abstenerse"),
        listing("dnc", mgmt={"notas": [{"content": "Propietario no quiere corredores."}]}),
        listing("price-invalid", price=78_000_000),
        listing("overbudget", price=105_000_000),
        listing("terms", mgmt={"estado_captacion": "No interesado",
                               "notas": [{"content": "No quiere pagar el 2%"}]}),
        listing("owned", mgmt={"estado_captacion": "En gestión",
                               "ejecutivo_id": "executive-other"}),
        listing("duplicate1", address="Esquilo 2706", price=75_000_000),
        listing("duplicate2", address="Esquilo 2706", price=75_000_000),
    ]
    docs[6]["precio_validacion"] = "INVALID_CLP_RANGE"
    db.propiedades_captacion.insert_many(docs)
    result = find_demand_captation_candidates(db, search(), limit=25)
    items = result["candidates"]
    ids = [x["listing_id"] for x in items]
    assert "good" in ids and "apartment" in ids
    assert "broker" not in ids and "broker-ai" not in ids
    assert "dnc" not in ids and "no-broker" not in ids
    assert "price-invalid" not in ids and "overbudget" not in ids
    assert ("duplicate1" in ids) != ("duplicate2" in ids)
    assert items[0]["type"] == "casa"
    assert next(x for x in items if x["listing_id"] == "terms")["decision"] == "ADMIN_REVIEW_TERMS"
    existing = next(x for x in items if x["listing_id"] == "owned")
    assert existing["decision"] == "EXISTING_MANAGEMENT"
    assert existing["previous_executive_id"] == "executive-other"
    assert result["excluded"]["EXCLUDED_DO_NOT_CONTACT"] == 2
    assert result["excluded"]["EXCLUDED_INVALID_PRICE"] == 1
    assert result["excluded"]["DUPLICATE"] == 1


def test_admin_queue_is_idempotent_and_does_not_send_or_assign():
    db = mongomock.MongoClient().sourcing
    lead_id = ObjectId()
    db.leads.insert_one({
        "_id": lead_id, "ejecutivo_asignado": "Hernán Castro",
        "lifecycle": {
            "assigned_to_user_id": "hernan-id",
            "current_assignment_cycle_id": "cycle1",
        },
        "commercial_intelligence": {"active_search": search()},
    })
    db.propiedades_captacion.insert_one(listing("good"))
    first = ensure_admin_demand_review(
        db, lead_id=lead_id, owner_id="hernan-id",
        cycle_id="cycle1", search=search(), now=NOW,
    )
    assert first and first == ensure_admin_demand_review(
        db, lead_id=lead_id, owner_id="hernan-id",
        cycle_id="cycle1", search=search(), now=NOW,
    )
    assert db.crm_tasks.count_documents({}) == 1
    task = db.crm_tasks.find_one({"_id": first})
    assert task["type"] == "DEMAND_SOURCING_ADMIN_REVIEW"
    assert task["recipient_role"] == "admin"
    assert task["buyer_executive_name"] == "Hernán Castro"
    assert task["contact_approved"] is False
    assert task["external_outbound_enabled"] is False
    assert len(task["candidate_refs"]) == 1
    assert "phone" not in str(task) and "telefono" not in str(task)
    assert db.propiedades_captacion.find_one({"listing_id": "good"})["gestion"] == {}


def test_admin_queue_respects_current_assignment_and_optout():
    db = mongomock.MongoClient().sourcing
    lead_id = ObjectId()
    db.leads.insert_one({
        "_id": lead_id,
        "lifecycle": {
            "assigned_to_user_id": "new-owner",
            "current_assignment_cycle_id": "new-cycle",
        },
        "commercial_intelligence": {"active_search": search()},
    })
    assert ensure_admin_demand_review(
        db, lead_id=lead_id, owner_id="old-owner",
        cycle_id="old-cycle", search=search(), now=NOW,
    ) is None
    disabled = search()
    disabled["opt_out"] = True
    assert ensure_admin_demand_review(
        db, lead_id=lead_id, owner_id="new-owner",
        cycle_id="new-cycle", search=disabled, now=NOW,
    ) is None
    assert db.crm_tasks.count_documents({}) == 0


def test_review_queue_is_gated_by_environment(monkeypatch):
    from chatbot.active_search_followup import ensure_followup_task
    db = mongomock.MongoClient().sourcing
    lead_id = ObjectId()
    db.leads.insert_one({
        "_id": lead_id, "phone": "+56911112222",
        "ejecutivo_asignado": "Hernán Castro",
        "lifecycle": {
            "assigned_to_user_id": "hernan-id",
            "current_assignment_cycle_id": "cycle1",
        },
        "commercial_intelligence": {"active_search": search()},
    })
    db.propiedades_captacion.insert_one(listing("good"))
    monkeypatch.delenv("PROCASA_DEMAND_SOURCING_ENABLED", raising=False)
    ensure_followup_task(db, lead_id=lead_id, cycle_id="cycle1",
                         owner_id="hernan-id", search=search(), now=NOW)
    assert db.crm_tasks.count_documents({}) == 1
    monkeypatch.setenv("PROCASA_DEMAND_SOURCING_ENABLED", "1")
    ensure_followup_task(db, lead_id=lead_id, cycle_id="cycle1",
                         owner_id="hernan-id", search=search(), now=NOW)
    assert db.crm_tasks.count_documents({}) == 2
    assert db.crm_tasks.count_documents({"recipient_role": "admin"}) == 1


def test_portal_undecided_duplicate_of_human_broker_is_vetoed():
    db = mongomock.MongoClient().sourcing
    undecided = listing("4110707", price=75_000_000)
    undecided["url"] = "https://www.toctoc.com/propiedades/casa/maipu/casas-esquilo-2706/4110707"
    known_broker = listing("112885892", price=75_000_000,
                           mgmt={"estado_captacion": "Corredor"})
    known_broker["source_portal"] = "chilepropiedades"
    known_broker["url"] = "https://chilepropiedades.cl/ver-publicacion/venta/maipu/casa/esquilo-2706/112885892"
    db.propiedades_captacion.insert_many([undecided, known_broker])
    out = find_demand_captation_candidates(db, search())
    assert out["candidates"] == []
    assert out["excluded"]["CROSS_PORTAL_VETO"] == 1
