from datetime import datetime, timezone

from owner_scoring import calculate_owner_score


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def test_no_signals_is_the_only_neutral_50_case():
    result = calculate_owner_score({}, NOW)
    assert result["owner_score"] == 50
    assert result["owner_score_signals"]["neutral"] is True


def test_particular_is_a_small_real_positive_signal():
    result = calculate_owner_score({"seller_type": "PARTICULAR"}, NOW)
    assert result["owner_score"] == 55


def test_first_person_counts_but_third_person_does_not():
    first = calculate_owner_score({"description": "Arriendo mi departamento", "seller_type": "PARTICULAR"}, NOW)
    third = calculate_owner_score({"description": "Propiedad de un solo dueño"}, NOW)
    assert first["owner_score"] == 90
    assert third["owner_score"] == 50


def test_commercial_signals_are_independent_from_state_and_confidence():
    result = calculate_owner_score({
        "classification": {"state": "DUEÑO_SEGURO", "confidence": 0.99},
        "seller_type": "PROFESIONAL", "broker_brand": "Rocamora Propiedades",
    }, NOW)
    assert result["owner_score"] < 50
    assert len(result["owner_score_signals"]["negative"]) == 2
