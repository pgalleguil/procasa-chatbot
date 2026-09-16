from __future__ import annotations

import pytest

from captacion_assignment_eligibility import calculate_assignment_eligibility


@pytest.fixture(autouse=True)
def _enable_phone_learning(monkeypatch):
    from config import Config

    monkeypatch.setattr(Config, "PHONE_LEARNING_ENABLED", True)


def _document(portal: str, state: str, *, assignment_ready: bool = False, probability=None) -> dict:
    classification = {
        "state": state,
        "assignment_ready": assignment_ready,
        "exclude_from_assignment": not assignment_ready,
        "source": "rules",
    }
    if probability is not None:
        classification["owner_probability"] = probability
    return {
        "_id": f"{portal}-1",
        "origen": portal,
        "listing_id": "listing-1",
        "title": "Casa en venta",
        "description": "Descripción suficiente",
        "comuna": "Santiago",
        "seller_name": "Corredor Conocido",
        "classification": classification,
    }


@pytest.mark.parametrize("portal", ["chilepropiedades", "yapo", "toctoc"])
def test_uncertain_without_known_phone_is_assignable_for_every_portal(portal):
    decision = calculate_assignment_eligibility(
        _document(portal, "INCIERTO"), contact_identity=None
    )

    assert decision["assignment_ready"] is True
    assert decision["assignment_block_reasons"] == []


@pytest.mark.parametrize("portal", ["chilepropiedades", "yapo", "toctoc"])
def test_uncertain_with_confirmed_broker_phone_is_blocked_for_every_portal(portal):
    decision = calculate_assignment_eligibility(
        _document(portal, "INCIERTO"),
        contact_identity={
            "phone_normalized": "56912345678",
            "status": "CORREDOR_CONFIRMED",
            "confirmed_corredor_count": 1,
        },
    )

    assert decision["assignment_ready"] is False
    assert "contact_identity_broker_confirmed" in decision["assignment_block_reasons"]


@pytest.mark.parametrize("portal", ["chilepropiedades", "yapo", "toctoc"])
@pytest.mark.parametrize("state", ["CORREDOR_PROBABLE", "CORREDOR_SEGURO"])
def test_broker_states_are_never_assignable(portal, state):
    decision = calculate_assignment_eligibility(
        _document(portal, state, assignment_ready=True, probability=0.99),
        contact_identity=None,
    )

    assert decision["assignment_ready"] is False
    assert "classification_not_assignable" in decision["assignment_block_reasons"]


@pytest.mark.parametrize("portal", ["chilepropiedades", "yapo", "toctoc"])
@pytest.mark.parametrize(
    ("state", "probability"),
    [("DUEÑO_SEGURO", 0.99), ("DUEÑO_PROBABLE", 0.80)],
)
def test_owner_states_are_assignable_without_confirmed_phone(portal, state, probability):
    decision = calculate_assignment_eligibility(
        _document(portal, state, assignment_ready=True, probability=probability),
        contact_identity=None,
    )

    assert decision["assignment_ready"] is True


@pytest.mark.parametrize("portal", ["chilepropiedades", "yapo", "toctoc"])
def test_same_name_with_different_or_unknown_phone_does_not_block(portal):
    decision = calculate_assignment_eligibility(
        _document(portal, "INCIERTO"), contact_identity=None
    )

    assert decision["assignment_ready"] is True
    assert "contact_identity_broker_confirmed" not in decision["assignment_block_reasons"]


def test_yapo_incierto_with_complete_deterministic_evidence_is_assignable():
    document = _document("yapo", "INCIERTO")
    document["classification"].update({
        "assignment_ready": False,
        "exclude_from_assignment": True,
        "assignment_block_reasons": ["NON_OWNER_STATE_NOT_ASSIGNABLE"],
        "version": "v5-rule-based",
        "reason": "Sin evidencia concluyente de propietario o corredor.",
        "evidence": ["seller_name"],
        "owner_probability": 0.55,
        "owner_probability_source": "deterministic_evidence_engine",
        "owner_probability_completeness": {"complete": True},
    })

    decision = calculate_assignment_eligibility(document, contact_identity=None)

    assert decision["assignment_ready"] is True
    assert decision["assignment_block_reasons"] == []


def test_toctoc_dataprop_publisher_is_never_assignable_even_when_state_is_uncertain():
    document = _document("toctoc", "INCIERTO")
    document.update({
        "publicador_visible": "DATAPROP.CL",
        "seller_name": "DATAPROP.CL",
        "seller_type": "DESCONOCIDO",
    })

    decision = calculate_assignment_eligibility(document, contact_identity=None)

    assert decision["assignment_ready"] is False
    assert "hard_broker_publisher_veto" in decision["assignment_block_reasons"]
    assert decision["effective_state"] == "CORREDOR_SEGURO"


def test_toctoc_corredora_profile_logo_is_hard_even_for_personal_display_name():
    document = _document("toctoc", "INCIERTO")
    document.update({
        "publicador_visible": "Paula Diaz",
        "seller_name": "Paula Diaz",
        "seller_profile_logo": "https://cdn.test/logos/corredora/123.png",
    })

    decision = calculate_assignment_eligibility(document, contact_identity=None)

    assert decision["assignment_ready"] is False
    assert "hard_broker_publisher_veto" in decision["assignment_block_reasons"]


@pytest.mark.parametrize("portal", ["chilepropiedades", "yapo", "toctoc"])
@pytest.mark.parametrize("state", ["AD_REMOVED", "BLOCKED", "INVALID"])
def test_invalid_removed_or_blocked_states_are_not_assignable(portal, state):
    decision = calculate_assignment_eligibility(
        _document(portal, state, assignment_ready=False), contact_identity=None
    )

    assert decision["assignment_ready"] is False
