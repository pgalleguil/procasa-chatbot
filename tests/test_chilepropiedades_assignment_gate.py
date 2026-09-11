from __future__ import annotations

import pytest

from captacion_assignment_eligibility import (
    ASSIGNMENT_GATE_VERSION,
    apply_assignment_eligibility_fields,
    assignment_classification_priority,
    calculate_assignment_eligibility,
)


@pytest.fixture(autouse=True)
def _enable_phone_learning(monkeypatch):
    from config import Config

    monkeypatch.setattr(Config, "PHONE_LEARNING_ENABLED", True)


def _document(state: str, *, probability: float = 0.99, seller_name: str = "Particular") -> dict:
    return {
        "_id": "cp-1",
        "origen": "chilepropiedades",
        "listing_id": "101",
        "title": "Casa en venta",
        "description": "Descripción suficiente",
        "comuna": "Santiago",
        "seller_name": seller_name,
        "telefono_normalizado": "56912345678",
        "classification": {
            "state": state,
            "owner_probability": probability,
            "assignment_ready": True,
            "exclude_from_assignment": False,
        },
    }


@pytest.mark.parametrize("state", ["DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"])
@pytest.mark.parametrize("probability", [0.50, 0.99])
def test_cp_assignable_states_ignore_owner_probability(state, probability):
    decision = calculate_assignment_eligibility(
        _document(state, probability=probability), contact_identity=None
    )

    assert decision["assignment_ready"] is True
    assert decision["exclude_from_assignment"] is False
    assert decision["effective_state"] == state
    assert decision["assignment_block_reasons"] == []


@pytest.mark.parametrize("state", ["CORREDOR_PROBABLE", "CORREDOR_SEGURO"])
def test_cp_broker_classifications_are_not_assignable(state):
    decision = calculate_assignment_eligibility(_document(state), contact_identity=None)

    assert decision["assignment_ready"] is False
    assert decision["exclude_from_assignment"] is True


def test_incierto_with_confirmed_broker_phone_is_not_assignable():
    decision = calculate_assignment_eligibility(
        _document("INCIERTO"),
        contact_identity={
            "phone_normalized": "56912345678",
            "status": "CORREDOR_CONFIRMED",
            "confirmed_corredor_count": 1,
        },
    )

    assert decision["assignment_ready"] is False
    assert "contact_identity_broker_confirmed" in decision["assignment_block_reasons"]
    assert decision["effective_state"] == "CORREDOR_SEGURO"


def test_owner_state_with_confirmed_broker_phone_is_not_assignable():
    decision = calculate_assignment_eligibility(
        _document("DUEÑO_SEGURO"),
        contact_identity={
            "phone_normalized": "56912345678",
            "status": "CORREDOR_CONFIRMED",
            "confirmed_corredor_count": 1,
        },
    )

    assert decision["assignment_ready"] is False
    assert "contact_identity_broker_confirmed" in decision["assignment_block_reasons"]


def test_same_name_without_confirmed_phone_identity_does_not_block_incierto():
    decision = calculate_assignment_eligibility(
        _document("INCIERTO", seller_name="Corredor Conocido"),
        contact_identity=None,
    )

    assert decision["assignment_ready"] is True


def test_apply_preserves_incierto_state_and_persists_new_eligibility():
    document = _document("INCIERTO", probability=0.99)

    decision = apply_assignment_eligibility_fields(document, contact_identity=None)

    assert decision["assignment_ready"] is True
    assert document["classification"]["state"] == "INCIERTO"
    assert document["classification"]["assignment_ready"] is True
    assert document["classification"]["exclude_from_assignment"] is False
    assert document["classification"]["eligibility_version"] == ASSIGNMENT_GATE_VERSION


def test_assignment_priority_is_deterministic_and_not_probability_score():
    docs = [_document("INCIERTO"), _document("DUEÑO_PROBABLE"), _document("DUEÑO_SEGURO")]

    ordered = sorted(docs, key=assignment_classification_priority)

    assert [doc["classification"]["state"] for doc in ordered] == [
        "DUEÑO_SEGURO",
        "DUEÑO_PROBABLE",
        "INCIERTO",
    ]
