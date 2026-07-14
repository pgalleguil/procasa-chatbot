from datetime import datetime, timezone

from owner_scoring import calculate_owner_score, compute_publisher_activity, publisher_identity_key


def test_no_signal_is_neutral_fallback():
    result = calculate_owner_score({"description": "Departamento luminoso"})
    assert result.score == 50
    assert result.useful_signal_count == 0


def test_explicit_owner_scores_above_neutral():
    result = calculate_owner_score({"description": "Soy el dueño y vendo mi casa"})
    assert result.score > 80
    assert {s["code"] for s in result.signals} >= {"OWNER_EXPLICIT", "FIRST_PERSON_OWNERSHIP"}


def test_third_person_owner_reference_is_not_publisher_evidence():
    for description in (
        "Propiedad vendida directamente por sus dueños",
        "Trato directo con el propietario",
        "Documentación de los propietarios al día",
    ):
        result = calculate_owner_score({"description": description})
        assert result.score == 50
        assert result.useful_signal_count == 0


def test_commercial_identity_scores_below_neutral():
    result = calculate_owner_score({
        "publicador": "María Patricia Cepeda",
        "company_name": "Grecop Corredores",
        "broker_brand": "Grecop Corredores",
        "seller_type": "Agente",
        "description": "Se arrienda departamento amoblado",
    })
    assert result.score == 0
    assert {s["code"] for s in result.signals} >= {
        "COMMERCIAL_IDENTITY", "PROFESSIONAL_SELLER_TYPE"
    }


def test_single_owner_legal_phrase_does_not_identify_publisher():
    result = calculate_owner_score({
        "description": "Propiedad de un solo dueño y sin deudas",
    })
    assert result.score == 50
    assert result.useful_signal_count == 0


def test_personal_identity_and_particular_are_weak_not_conclusive():
    result = calculate_owner_score({
        "publicador": "Juan Pérez",
        "seller_type": "Particular",
        "description": "Se vende departamento",
    })
    assert result.score == 62


def test_owner_type_badge_is_positive_but_not_conclusive():
    result = calculate_owner_score({
        "publicador": "Enrique Ceballos",
        "seller_type": "Propietario",
        "description": "Se vende departamento",
    })
    assert result.score == 65


def test_business_like_two_word_identity_is_not_a_person_name():
    result = calculate_owner_score({
        "publicador": "GreenHidropónico Alimento",
        "seller_type": "Propietario",
        "description": "Vendo mi departamento",
    })
    assert result.score == 88
    assert "PERSONAL_IDENTITY" not in {s["code"] for s in result.signals}


def test_multi_publisher_reduces_score():
    result = calculate_owner_score({
        "publicador": "Juan Pérez", "seller_type": "Particular",
        "publisher_activity": {
            "unique_properties": 8, "reposts_same_property": 2,
            "window_days": 90,
        },
    })
    assert result.score < 50


def test_reposts_of_same_property_do_not_trigger_broker_penalty():
    result = calculate_owner_score({
        "publisher_activity": {
            "unique_properties": 1, "reposts_same_property": 12,
            "window_days": 30,
        },
    })
    assert result.score == 50
    assert result.useful_signal_count == 0


def test_legacy_count_without_time_window_is_not_penalized():
    result = calculate_owner_score({"multi_publisher_count": 20})
    assert result.score == 50


def test_publisher_activity_separates_reposts_from_distinct_properties():
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    current = {"seller_profile_id": "abc", "listing_id": "p1", "processed_at": now}
    history = [
        {"seller_profile_id": "abc", "listing_id": "p1", "processed_at": now},
        {"seller_profile_id": "abc", "listing_id": "p2", "processed_at": now},
        {"seller_profile_id": "other", "listing_id": "p3", "processed_at": now},
    ]
    result = compute_publisher_activity(current, history, now=now)
    assert result["unique_properties"] == 2
    assert result["reposts_same_property"] == 1
    assert result["total_publications"] == 3


def test_generic_portal_label_is_not_a_shared_publisher_identity():
    assert publisher_identity_key({"publicador_visible": "Particular"}) == ""
    assert publisher_identity_key({"publicador_visible": "Agente"}) == ""
