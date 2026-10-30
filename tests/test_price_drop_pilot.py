"""Isolated, no-provider regression tests for the price-drop WhatsApp pilot."""
from datetime import datetime, timedelta, timezone

import pytest

from chatbot.price_drop_pilot import (
    _other_property_referenced,
    context_instruction,
    load_active_context,
    validate_record,
)


class Cursor(list):
    def limit(self, count):
        return Cursor(self[:count])


class Collection:
    def __init__(self, rows=None, one=None):
        self.rows = rows or []
        self.one = one
        self.queries = []

    def find(self, query, projection=None):
        self.queries.append(query)
        return Cursor(self.rows)

    def find_one(self, query, *args, **kwargs):
        self.queries.append(query)
        return self.one


def sale(price=2690, active=True, is_sale=True, office="PROCASA SUCRE"):
    return {
        "codigo": "6436",
        "disponible_prop360": active,
        "estado": {"oficina": office},
        "tipo_operacion": {"venta": is_sale, "precio_venta": {"precio_uf": price}},
    }


def record(lead_id="lead-1", **changes):
    now = datetime.now(timezone.utc)
    result = {
        "_id": "price-drop-test-1",
        "phone_normalized": "+56977770000",
        "status": "SENT",
        "provider_message_id": "provider-123",
        "lead_id": lead_id,
        "property_code": "6436",
        "operation": "Venta",
        "old_price_uf": 2790.0,
        "new_price_uf": 2690.0,
        "sent_at": now - timedelta(hours=1),
        "expires_at": now + timedelta(days=1),
    }
    result.update(changes)
    return result


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setenv("PRICE_DROP_PILOT_ENABLED", "1")
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", "+56977770000")


def fake_db(leads=None, campaign=None, prop=None):
    return {
        "leads": Collection(rows=leads if leads is not None else [{"_id": "lead-1"}]),
        "crm_price_drop_pilot_v1": Collection(one=campaign if campaign is not None else record()),
        "universo_cartera_prop360": Collection(one=prop if prop is not None else sale()),
    }


def test_pilot_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PRICE_DROP_PILOT_ENABLED", raising=False)
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", "+56977770000")
    db = fake_db()
    assert load_active_context(db, "+56977770000", "Sí, me interesa") is None
    assert not db["leads"].queries


def test_never_runs_for_another_phone(allowed):
    db = fake_db()
    assert load_active_context(db, "+56977770001", "Sí") is None
    assert not db["leads"].queries


def test_duplicate_lead_identity_fails_closed(allowed):
    db = fake_db(leads=[{"_id": "lead-1"}, {"_id": "lead-2"}])
    assert load_active_context(db, "+56977770000", "Sí, me interesa") is None
    assert not db["crm_price_drop_pilot_v1"].queries


def test_valid_delivered_context_for_short_yes(allowed):
    db = fake_db()
    result = load_active_context(db, "+56977770000", "Sí, me interesa")
    assert result["property_code"] == "6436"
    assert result["old_price_uf"] == 2790
    assert result["new_price_uf"] == 2690
    assert "no confirma una visita" in context_instruction(result).lower() or "NO confirma una visita" in context_instruction(result)


@pytest.mark.parametrize("changes", [
    {"status": "SENDING"},
    {"status": "DELIVERY_UNKNOWN"},
    {"provider_message_id": None},
    {"lead_id": "another-lead"},
    {"operation": "Arriendo"},
    {"old_price_uf": 2600},
    {"new_price_uf": 5100},
    {"phone_normalized": "+56977770001"},
])
def test_bad_campaign_record_rejected(changes):
    assert not validate_record(record(**changes), sale(), "lead-1")


@pytest.mark.parametrize("bad_property", [
    sale(price=2650),
    sale(active=False),
    sale(is_sale=False),
    sale(office="OTHER OFFICE"),
    {**sale(), "codigo": "9999"},
])
def test_stale_or_unavailable_property_rejected(bad_property):
    assert not validate_record(record(), bad_property, "lead-1")


def test_new_explicit_property_takes_priority(allowed):
    db = fake_db()
    assert load_active_context(db, "+56977770000", "Quiero saber del código 6500") is None
    assert load_active_context(db, "+56977770000", "https://www.procasa.cl/6500") is None


def test_current_campaign_property_explicitly_named_is_permitted(allowed):
    db = fake_db()
    assert load_active_context(db, "+56977770000", "Consulta por el código 6436")


def test_other_ref_filter_does_not_interpret_price_as_property_code():
    assert not _other_property_referenced("¿Bajó de UF 2790 a 2690?", "6436")
    assert _other_property_referenced("código 6500", "6436")


def test_expired_context_rejected(allowed):
    db = fake_db(campaign=record(expires_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    assert load_active_context(db, "+56977770000", "sí") is None


def test_not_yet_sent_context_rejected(allowed):
    db = fake_db(campaign=record(sent_at=datetime.now(timezone.utc) + timedelta(hours=1)))
    assert load_active_context(db, "+56977770000", "sí") is None


def test_short_other_code_rejected(allowed):
    db = fake_db()
    assert load_active_context(db, "+56977770000", "6500") is None
