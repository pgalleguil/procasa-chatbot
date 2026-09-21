from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from api_captacion import (
    _atomic_assign_distribution_candidate,
    _build_bounded_distribution_plan,
    _has_broker_identity,
)
from captacion_distribution import active_workable_backlogs, is_captacion_distribution_executive


def _property(property_id: int, state: str = "INCIERTO", *, days_old: int = 0) -> dict:
    return {
        "_id": str(property_id),
        # Generic bounded-distribution fixtures use Yapo so an INCIERTO
        # record exercises the pre-existing cross-portal policy.  TOCTOC
        # INCIERTO is covered separately and is intentionally blocked.
        "origen": "yapo",
        "listing_id": f"listing-{property_id}",
        "title": "Casa en venta",
        "description": "Descripción suficiente",
        "comuna_slug": "santiago",
        "created_at": datetime.now(timezone.utc) - timedelta(days=days_old),
        "classification": {"state": state, "source": "rules"},
        "pipeline_complete": True,
    }


def _agents(*names: str) -> list[dict]:
    return [
        {"id": f"agent-{index}", "name": name, "comunas_interes_norm": ["santiago"]}
        for index, name in enumerate(names, start=1)
    ]


def test_batch_cap_limits_3000_eligible_properties_to_100():
    props = [_property(index) for index in range(3000)]
    agents = _agents("Ana", "Beto")

    plan, stats = _build_bounded_distribution_plan(
        props,
        agents,
        {"agent-1": 0, "agent-2": 0},
        batch_size=100,
        max_per_agent=100,
        max_open=350,
    )

    assert len(plan) == 100
    assert stats["selected"] == 100
    assert len({prop["_id"] for prop, _agent in plan}) == 100


def test_second_run_takes_next_properties_without_duplicates():
    props = [_property(index) for index in range(300)]
    agents = _agents("Ana", "Beto")

    first, _ = _build_bounded_distribution_plan(
        props, agents, {"agent-1": 0, "agent-2": 0},
        batch_size=100, max_per_agent=100, max_open=350,
    )
    first_ids = {prop["_id"] for prop, _agent in first}
    remaining = [prop for prop in props if prop["_id"] not in first_ids]
    second, _ = _build_bounded_distribution_plan(
        remaining, agents, {"agent-1": 50, "agent-2": 50},
        batch_size=100, max_per_agent=100, max_open=350,
    )

    assert len(second) == 100
    assert first_ids.isdisjoint({prop["_id"] for prop, _agent in second})


def test_capacity_blocks_an_executive_at_open_workload_limit():
    plan, stats = _build_bounded_distribution_plan(
        [_property(1)],
        _agents("Ana"),
        {"agent-1": 350},
        batch_size=14,
        max_per_agent=2,
        max_open=350,
    )

    assert plan == []
    assert stats["capacity"] == 1


def test_workable_backlog_prioritizes_zero_over_open_workload():
    props = [_property(1)]
    agents = _agents("Cero", "Cincuenta")

    plan, _ = _build_bounded_distribution_plan(
        props,
        agents,
        {"agent-1": 106, "agent-2": 137},
        batch_size=14,
        max_per_agent=2,
        max_open=350,
        active_workable_backlog={"agent-1": 0, "agent-2": 50},
    )

    assert plan[0][1] == "agent-1"


def test_workable_backlog_prioritizes_twelve_over_one_hundred_thirty_seven():
    props = [_property(1)]
    agents = _agents("Doce", "Ciento Treinta y Siete")

    plan, _ = _build_bounded_distribution_plan(
        props,
        agents,
        {"agent-1": 12, "agent-2": 137},
        batch_size=14,
        max_per_agent=2,
        max_open=350,
        active_workable_backlog={"agent-1": 12, "agent-2": 137},
    )

    assert plan[0][1] == "agent-1"


def test_workable_backlog_does_not_bypass_territorial_compatibility():
    props = [_property(1)]
    props[0]["comuna_slug"] = "las-condes"
    agents = [
        {"id": "agent-1", "name": "Cero", "comunas_interes_norm": ["santiago"]},
        {"id": "agent-2", "name": "Cincuenta", "comunas_interes_norm": ["las-condes"]},
    ]

    plan, _ = _build_bounded_distribution_plan(
        props,
        agents,
        {"agent-1": 0, "agent-2": 50},
        batch_size=14,
        max_per_agent=2,
        max_open=350,
        active_workable_backlog={"agent-1": 0, "agent-2": 50},
    )

    assert plan[0][1] == "agent-2"


def test_active_workable_backlog_uses_human_ledger_and_excludes_terminal_or_legacy():
    import mongomock
    from config import Config

    db = mongomock.MongoClient()["test"]
    agent_id = "agent-1"
    db["usuarios"].insert_one({"_id": agent_id, "rol": "agente", "is_active": True})
    coll = db[Config.CAPTACION_COLLECTION_NAME]
    base = _property(1)
    base["gestion"] = {"estado": "NUEVO", "ejecutivo_id": agent_id}
    managed = _property(2)
    managed["gestion"] = {"estado": "NUEVO", "ejecutivo_id": agent_id}
    legacy = _property(3)
    legacy["gestion"] = {"estado": "En gestión", "ejecutivo_id": agent_id}
    terminal = _property(4)
    terminal["gestion"] = {"estado": "Duplicado", "ejecutivo_id": agent_id}
    captured = _property(5)
    captured["gestion"] = {"estado": "Captado", "ejecutivo_id": agent_id}
    broker = _property(6)
    broker["gestion"] = {"estado": "Corredor", "ejecutivo_id": agent_id}
    coll.insert_many([base, managed, legacy, terminal, captured, broker])
    db["captacion_management_events"].insert_one({
        "event_type": "manual_decision_confirmed",
        "credited": True,
        "property_id": "2",
    })
    original = Config.PHONE_LEARNING_ENABLED
    Config.PHONE_LEARNING_ENABLED = False
    try:
        assert active_workable_backlogs(db, [agent_id]) == {agent_id: 1}
    finally:
        Config.PHONE_LEARNING_ENABLED = original


def test_active_workable_backlog_excludes_phone_confirmed_identity(monkeypatch):
    import mongomock
    from config import Config

    db = mongomock.MongoClient()["test"]
    agent_id = "agent-phone"
    db["usuarios"].insert_one({"_id": agent_id, "rol": "agente", "is_active": True})
    candidate = _property(10)
    candidate["gestion"] = {"estado": "NUEVO", "ejecutivo_id": agent_id}
    candidate["telefono_normalizado"] = "56912345678"
    db[Config.CAPTACION_COLLECTION_NAME].insert_one(candidate)
    db["captacion_contact_identity"].insert_one({
        "phone_normalized": "56912345678",
        "status": "CORREDOR_CONFIRMED",
        "confirmed_corredor_count": 1,
    })
    monkeypatch.setattr(Config, "PHONE_LEARNING_ENABLED", True)

    assert active_workable_backlogs(db, [agent_id]) == {agent_id: 0}


def test_privileged_roles_require_explicit_captacion_opt_in():
    assert is_captacion_distribution_executive({"rol": "agente"}) is True
    assert is_captacion_distribution_executive({"rol": "admin"}) is False
    assert is_captacion_distribution_executive({"rol": "supervisor"}) is False
    assert is_captacion_distribution_executive({"rol": "jefatura"}) is False
    assert is_captacion_distribution_executive({"rol": "admin", "captacion_enabled": True}) is True


def test_owner_priority_precedes_recent_uncertain():
    props = [
        _property(1, "INCIERTO", days_old=0),
        _property(2, "DUEÑO_PROBABLE", days_old=30),
        _property(3, "DUEÑO_SEGURO", days_old=60),
    ]
    plan, _ = _build_bounded_distribution_plan(
        props, _agents("Ana"), {"agent-1": 0},
        batch_size=3, max_per_agent=3, max_open=350,
    )

    assert [prop["classification"]["state"] for prop, _agent in plan] == [
        "DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"
    ]


def test_same_name_is_not_a_commercial_identity_block():
    document = _property(1)
    document["seller_name"] = "Corredor Conocido"
    document["contact_name"] = "Corredor Conocido"

    assert _has_broker_identity(document) is False


def test_atomic_guard_reports_concurrent_assignment_without_duplicate():
    from config import Config

    class FakeEvents:
        def find_one(self, _query):
            return None

    class FakeCollection:
        def find_one(self, _query):
            return fake_property

        def update_one(self, _query, _update):
            return SimpleNamespace(modified_count=0)

    original = Config.PHONE_LEARNING_ENABLED
    Config.PHONE_LEARNING_ENABLED = False
    fake_property = _property(1)
    fake_property["_id"] = "507f1f77bcf86cd799439011"
    try:
        status = _atomic_assign_distribution_candidate(
            db=object(),
            coll=FakeCollection(),
            events_coll=FakeEvents(),
            document=fake_property,
            agent=_agents("Ana")[0],
            now=datetime.now(timezone.utc),
        )
    finally:
        Config.PHONE_LEARNING_ENABLED = original

    assert status == "already_assigned"


def test_atomic_recheck_skips_phone_confirmed_between_selection_and_write():
    from config import Config

    class FakeEvents:
        def find_one(self, _query):
            return None

    class FakeIdentityCollection:
        def find_one(self, _query):
            return {
                "phone_normalized": "56912345678",
                "status": "CORREDOR_CONFIRMED",
                "confirmed_corredor_count": 1,
            }

    class FakeDb:
        def __getitem__(self, name):
            assert name == "captacion_contact_identity"
            return FakeIdentityCollection()

    class FakeCollection:
        def find_one(self, _query):
            fresh = _property(2)
            fresh["_id"] = "507f1f77bcf86cd799439012"
            fresh["telefono_normalizado"] = "56912345678"
            return fresh

        def update_one(self, *_args):
            raise AssertionError("phone-confirmed candidate must not reach update_one")

    original = Config.PHONE_LEARNING_ENABLED
    Config.PHONE_LEARNING_ENABLED = True
    try:
        document = _property(2)
        document["_id"] = "507f1f77bcf86cd799439012"
        document["telefono_normalizado"] = "56912345678"
        status = _atomic_assign_distribution_candidate(
            db=FakeDb(),
            coll=FakeCollection(),
            events_coll=FakeEvents(),
            document=document,
            agent=_agents("Ana")[0],
            now=datetime.now(timezone.utc),
        )
    finally:
        Config.PHONE_LEARNING_ENABLED = original

    assert status == "phone"
