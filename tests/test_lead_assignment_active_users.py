"""Regression tests: inactive CRM users must not receive new lead assignments."""

from bson import ObjectId
import mongomock
import pytest

from chatbot import lead_router
from chatbot.constants import UNASSIGNED_LABEL
from chatbot.crm_metrics import create_assignment_cycle


@pytest.fixture
def routing_db(monkeypatch):
    db = mongomock.MongoClient().URLS
    monkeypatch.setattr(lead_router, "get_db", lambda: db)
    monkeypatch.setattr(
        lead_router, "find_property_by_any_identifier",
        lambda *_args, **_kwargs: {"codigo": "6436"},
    )
    monkeypatch.setattr(
        lead_router, "get_prop_executive",
        lambda _prop: "Rocío Aliaga",
    )
    monkeypatch.setattr(
        lead_router, "get_prop_location",
        lambda _prop: {"region": "Valparaíso", "comuna": "Viña del Mar"},
    )
    return db


def _agent(db, name, *, active, communes=("Viña del Mar",)):
    return db.usuarios.insert_one({
        "_id": ObjectId(),
        "nombre": name,
        "rol": "agente",
        "is_active": active,
        "telefono": "+56911111111",
        "comunas_interes": list(communes),
    }).inserted_id


def test_inactive_property_owner_never_assigned_even_with_phone(routing_db):
    _agent(routing_db, "Rocío Aliaga", active=False)
    name, phone, kind = lead_router.find_responsible_executive(property_code="6436")
    assert (name, phone, kind) == (UNASSIGNED_LABEL, None, "NO_ELIGIBLE_EXECUTIVE")


def test_inactive_property_owner_uses_active_same_commune_replacement(routing_db):
    _agent(routing_db, "Rocío Aliaga", active=False)
    _agent(routing_db, "Equipo Valparaíso", active=True)
    name, phone, kind = lead_router.find_responsible_executive(property_code="6436")
    assert name == "Equipo Valparaíso"
    assert phone == "+56911111111"
    assert kind == "PROPERTY"


def test_active_owner_keeps_direct_property_assignment(routing_db):
    _agent(routing_db, "Rocío Aliaga", active=True)
    name, phone, kind = lead_router.find_responsible_executive(property_code="6436")
    assert (name, phone, kind) == ("Rocío Aliaga", "+56911111111", "PROPERTY")


def test_availability_change_is_checked_on_every_route(routing_db):
    user_id = _agent(routing_db, "Rocío Aliaga", active=True)
    assert lead_router.find_responsible_executive(property_code="6436")[0] == "Rocío Aliaga"
    routing_db.usuarios.update_one({"_id": user_id}, {"$set": {"is_active": False}})
    assert lead_router.find_responsible_executive(property_code="6436")[0] == UNASSIGNED_LABEL


def test_rr_skips_inactive_user_and_has_no_unsafe_fallback(routing_db):
    _agent(routing_db, "Mariela Arriagada", active=False)
    _agent(routing_db, "Hernán Castro", active=False)
    _agent(routing_db, "María Paz Galleguillos", active=False)
    assert lead_router.get_next_round_robin_executive("Ñuñoa") == UNASSIGNED_LABEL
    assert routing_db.lead_routing_state.count_documents({}) == 0
    _agent(routing_db, "Hernán Castro", active=True)
    assert lead_router.get_next_round_robin_executive("Ñuñoa") == "Hernán Castro"


def test_inactive_user_cannot_receive_new_assignment_cycle(routing_db):
    user_id = _agent(routing_db, "Rocío Aliaga", active=False)
    lead_id = ObjectId()
    routing_db.leads.insert_one({"_id": lead_id, "phone": "56911111111"})
    with pytest.raises(ValueError, match="inactive user"):
        create_assignment_cycle(
            routing_db,
            lead={"_id": lead_id},
            assigned_to_user_id=str(user_id),
            assigned_to_display_name="Rocío Aliaga",
            assigned_by="test",
            reason="test",
        )
    assert routing_db.crm_assignment_cycles.count_documents({}) == 0

def test_active_lookup_accepts_pymongo_style_database_without_bool(routing_db):
    """PyMongo Database raises NotImplementedError for boolean evaluation."""
    _agent(routing_db, "Rocío Aliaga", active=True)

    class DatabaseWithoutTruthValue:
        def __bool__(self):
            raise NotImplementedError("Database objects do not implement truth value testing")

        def __getitem__(self, name):
            return routing_db[name]

    user = lead_router._active_agent_record("Rocío Aliaga", db=DatabaseWithoutTruthValue())
    assert user is not None
    assert user["nombre"] == "Rocío Aliaga"
