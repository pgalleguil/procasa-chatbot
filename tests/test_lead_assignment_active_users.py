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


def _sla_score_row(user_id, name, *, speed, sla, backlog=3):
    return {
        "user_id": str(user_id), "executive": name,
        "identity_key": name.lower(), "executive_key": name.lower(),
        "active": True, "role": "agente", "legacy": False,
        "sample_size": 40, "sla_compliance_rate": sla,
        "attention_rate": sla,
        "p50_first_management_business_minutes": speed,
        "p90_first_management_business_minutes": speed * 2,
        "open_current_policy": backlog,
        "unmanaged_current_policy": backlog,
        "expired_current_policy": 0,
        "shadow_received_count": 0,
    }


def _seed_sla_snapshot(db, rows):
    from datetime import datetime, timezone
    db.crm_assignment_cycles.insert_one({
        "assigned_by": "sla_reassignment",
        "assigned_at": datetime.now(timezone.utc),
        "cycle_status": "closed",
        "unassigned_at": datetime.now(timezone.utc),
        "candidate_scores_snapshot": rows,
    })


def test_inactive_rocio_uses_global_sla_ranking_not_commune_round_robin(routing_db):
    """No Valparaíso agent: use SLA score across the active commercial team."""
    _agent(routing_db, "Rocío Aliaga", active=False)
    erika = _agent(routing_db, "Erika Garrido", active=True, communes=("La Florida",))
    susana = _agent(routing_db, "Susana Ensignia", active=True, communes=("Ñuñoa",))
    _seed_sla_snapshot(routing_db, [
        _sla_score_row(erika, "Erika Garrido", speed=10, sla=0.95, backlog=2),
        _sla_score_row(susana, "Susana Ensignia", speed=80, sla=0.30, backlog=80),
    ])
    name, phone, kind = lead_router.find_responsible_executive(property_code="6436")
    assert (name, phone, kind) == ("Erika Garrido", "+56911111111", "PROPERTY")


def test_inactive_rocio_uses_frozen_tier_priority_when_hernan_eligible(routing_db):
    """The same frozen SLA Tier 1 preference applies, not a parallel policy."""
    _agent(routing_db, "Rocío Aliaga", active=False)
    hernan = _agent(routing_db, "Hernán Castro", active=True, communes=("Maipú",))
    erika = _agent(routing_db, "Erika Garrido", active=True, communes=("La Florida",))
    _seed_sla_snapshot(routing_db, [
        _sla_score_row(hernan, "Hernán Castro", speed=12, sla=0.92, backlog=4),
        _sla_score_row(erika, "Erika Garrido", speed=90, sla=0.34, backlog=60),
    ])
    assert lead_router.find_responsible_executive(property_code="6436")[0] == "Hernán Castro"


def test_sla_score_never_resurrects_inactive_agent(routing_db):
    _agent(routing_db, "Rocío Aliaga", active=False)
    erika = _agent(routing_db, "Erika Garrido", active=False, communes=("La Florida",))
    susana = _agent(routing_db, "Susana Ensignia", active=True, communes=("Ñuñoa",))
    _seed_sla_snapshot(routing_db, [
        _sla_score_row(erika, "Erika Garrido", speed=5, sla=1, backlog=0),
        _sla_score_row(susana, "Susana Ensignia", speed=90, sla=0.35, backlog=70),
    ])
    assert lead_router.find_responsible_executive(property_code="6436")[0] == "Susana Ensignia"


def test_no_recent_sla_snapshot_still_assigns_to_active_staff(routing_db):
    """Availability beats territorial vacancy, even when scores are missing."""
    _agent(routing_db, "Rocío Aliaga", active=False)
    _agent(routing_db, "Erika Garrido", active=True, communes=("La Florida",))
    result = lead_router.find_responsible_executive(property_code="6436")
    assert result == ("Erika Garrido", "+56911111111", "PROPERTY")


def test_routing_excludes_active_administrator_mislabelled_agent(routing_db):
    _agent(routing_db, "Rocío Aliaga", active=False)
    hernan = _agent(routing_db, "Hernán Castro", active=True, communes=("Maipú",))
    from chatbot.crm_sla_policy_freeze import RM_ADMIN_EXCLUDED_USER_IDS
    administrator_id = ObjectId(next(iter(RM_ADMIN_EXCLUDED_USER_IDS)))
    routing_db.usuarios.insert_one({
        "_id": administrator_id,
        "nombre": "Administrador", "rol": "agente",
        "is_active": True, "telefono": "+56922222222",
    })
    _seed_sla_snapshot(routing_db, [
        _sla_score_row(administrator_id, "Administrador", speed=3, sla=1, backlog=0),
        _sla_score_row(hernan, "Hernán Castro", speed=80, sla=0.31, backlog=20),
    ])
    assert lead_router.find_responsible_executive(property_code="6436")[0] == "Hernán Castro"
