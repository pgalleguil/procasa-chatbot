"""Tests: SLA release and distribution never touch managed contacts."""
import sys, os
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
import mongomock
from bson import ObjectId

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _base(listing_id="1000", estado="NUEVO", fecha_ultima_gestion=None, asignacion_days=10, **kw):
    g = {
        "ejecutivo_id": "6a681413140190dde11f26d1",
        "ejecutivo_asignado": "Hernán Castro",
        "estado": estado,
        "fecha_asignacion": datetime.now(timezone.utc) - timedelta(days=asignacion_days),
    }
    if fecha_ultima_gestion is not None:
        g["fecha_ultima_gestion"] = fecha_ultima_gestion
    g.update(kw.get("gestion_extra", {}))
    doc = {
        "_id": ObjectId(),
        "listing_id": listing_id,
        "origen": "toctoc",
        "comuna": "Maipu",
        "comuna_slug": "maipu",
        "title": "test",
        "description": "test descripcion suficiente",
        "classification": {
            "state": "INCIERTO",
            "assignment_ready": True,
            "exclude_from_assignment": False,
            "owner_probability": 0.6,
            "source": "structural_rules",
        },
        "gestion": g,
    }
    doc.update(kw.get("doc_extra", {}))
    return doc


def _events_coll(mongo, listing_id="1000"):
    return mongo["test_db"]["captacion_management_events"]


# ===== has_management_evidence guard (release) =====

def test_release_skips_doc_with_notes():
    from redistribute_captacion import has_management_evidence
    doc = _base(gestion_extra={"notas": [{"texto": "llamado"}]})
    has_ev, reason = has_management_evidence(doc, None)
    assert has_ev is True
    assert "notas" in reason


def test_release_skips_doc_with_management_event():
    from redistribute_captacion import has_management_evidence
    mongo = mongomock.MongoClient()
    events = mongo["test_db"]["captacion_management_events"]
    events.insert_one({"listing_id": "1000", "action": "whatsapp", "channel": "wa"})
    doc = _base()
    has_ev, reason = has_management_evidence(doc, events)
    assert has_ev is True
    assert "event" in reason


def test_release_allows_doc_without_evidence():
    from redistribute_captacion import has_management_evidence
    doc = _base()
    has_ev, reason = has_management_evidence(doc, None)
    assert has_ev is False


def test_release_skips_doc_with_fecha_ultima_gestion():
    from redistribute_captacion import has_management_evidence
    doc = _base(fecha_ultima_gestion=datetime.now(timezone.utc) - timedelta(days=1))
    has_ev, reason = has_management_evidence(doc, None)
    assert has_ev is True
    assert "fecha_ultima_gestion" in reason


# ===== distribute_sourced_leads guard (integration) =====

def test_distribution_skips_managed_contacts():
    """Un contacto con nota no debe re-asignarse aunque ejecutivo_id sea None."""
    from api_captacion import distribute_sourced_leads
    from bson import ObjectId
    mongo = mongomock.MongoClient()
    db = mongo["test_db"]

    agente = {"_id": ObjectId("6a681413140190dde11f26d1"),
              "nombre": "Hernán Castro", "rol": "agente", "is_active": True,
              "comunas_interes_norm": ["maipu"]}
    db["usuarios"].insert_one(agente)

    # Contacto con nota (gestión previa) pero sin ejecutivo_id -> protegido
    db["propiedades_captacion"].insert_one(_base(listing_id="managed_1", gestion_extra={
        "ejecutivo_id": None, "notas": [{"texto": "llamado"}]}))
    # Contacto sin gestión -> distribuible
    db["propiedades_captacion"].insert_one(_base(listing_id="fresh_1", gestion_extra={
        "ejecutivo_id": None}))

    with patch("api_captacion.get_db", return_value=db), \
         patch("api_captacion.get_captacion_collection", return_value=db["propiedades_captacion"]), \
         patch("api_captacion.new_assignment_cycle", return_value={"assignment_cycle_id": "c1"}):
        assigned = distribute_sourced_leads()

    assert assigned == 1
    doc = db["propiedades_captacion"].find_one({"listing_id": "managed_1"})
    assert doc["gestion"]["ejecutivo_id"] is None  # protegido
    doc2 = db["propiedades_captacion"].find_one({"listing_id": "fresh_1"})
    assert doc2["gestion"]["ejecutivo_id"] == str(agente["_id"])


def test_distribution_skips_contacts_with_event():
    from api_captacion import distribute_sourced_leads
    from bson import ObjectId
    mongo = mongomock.MongoClient()
    db = mongo["test_db"]
    db["captacion_management_events"].insert_one({"listing_id": "ev_1", "action": "call"})

    agente = {"_id": ObjectId("6a681413140190dde11f26d1"),
              "nombre": "Hernán Castro", "rol": "agente", "is_active": True,
              "comunas_interes_norm": ["maipu"]}
    db["usuarios"].insert_one(agente)
    db["propiedades_captacion"].insert_one(_base(listing_id="ev_1", gestion_extra={"ejecutivo_id": None}))

    with patch("api_captacion.get_db", return_value=db), \
         patch("api_captacion.get_captacion_collection", return_value=db["propiedades_captacion"]), \
         patch("api_captacion.new_assignment_cycle", return_value={"assignment_cycle_id": "c1"}):
        assigned = distribute_sourced_leads()

    assert assigned == 0
    doc = db["propiedades_captacion"].find_one({"listing_id": "ev_1"})
    assert doc["gestion"]["ejecutivo_id"] is None


# ===== release_stale_captaciones guard (integration) =====

def test_release_does_not_free_managed_contact():
    from api_captacion import release_stale_captaciones
    mongo = mongomock.MongoClient()
    db = mongo["test_db"]
    coll = db["propiedades_captacion"]

    # Doc gestionado (notas) viejo -> NO se libera
    coll.insert_one(_base(listing_id="g_1", gestion_extra={"notas": [{"texto": "x"}]}))
    # Doc sin gestion viejo -> se libera
    coll.insert_one(_base(listing_id="s_1"))

    with patch("api_captacion.get_db", return_value=db), \
         patch("api_captacion.get_captacion_collection", return_value=coll):
        released = release_stale_captaciones(sla_dias=5)

    assert released == 1
    assert coll.find_one({"listing_id": "s_1"})["gestion"]["liberada_por_sla"] is True
    assert coll.find_one({"listing_id": "s_1"})["gestion"]["ejecutivo_id"] is None
    assert "liberada_por_sla" not in coll.find_one({"listing_id": "g_1"})["gestion"]


def test_release_does_not_free_fresh_assignment():
    from api_captacion import release_stale_captaciones
    mongo = mongomock.MongoClient()
    db = mongo["test_db"]
    coll = db["propiedades_captacion"]
    coll.insert_one(_base(listing_id="fresh", asignacion_days=1))

    with patch("api_captacion.get_db", return_value=db), \
         patch("api_captacion.get_captacion_collection", return_value=coll):
        released = release_stale_captaciones(sla_dias=5)

    assert released == 0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
