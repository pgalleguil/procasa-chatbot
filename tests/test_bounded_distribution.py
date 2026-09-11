from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from api_captacion import (
    _atomic_assign_distribution_candidate,
    _build_bounded_distribution_plan,
    _has_broker_identity,
)


def _property(property_id: int, state: str = "INCIERTO", *, days_old: int = 0) -> dict:
    return {
        "_id": str(property_id),
        "origen": "toctoc",
        "listing_id": f"listing-{property_id}",
        "title": "Casa en venta",
        "description": "Descripción suficiente",
        "comuna_slug": "santiago",
        "created_at": datetime.now(timezone.utc) - timedelta(days=days_old),
        "classification": {"state": state, "source": "rules"},
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
