from captacion_assignment_eligibility import assignment_eligibility


def base_doc():
    return {
        "listing_id": "1", "url": "https://example/1", "comuna": "Talca",
        "description": "Descripción suficiente de la propiedad.",
        "classification": {"state": "INCIERTO", "source": "deepseek", "deepseek_status": "VALID",
                           "deepseek_raw": {"choices": [{}]}, "assignment_ready": True,
                           "owner_probability": 0.75},
    }


def test_valid_persisted_deepseek_can_be_assigned():
    assert assignment_eligibility(base_doc()) == (True, [])


def test_pending_or_unpersisted_deepseek_is_blocked():
    doc = base_doc(); doc["classification"]["deepseek_status"] = "TIMEOUT"
    assert not assignment_eligibility(doc)[0]
    doc = base_doc(); doc["classification"]["deepseek_raw"] = {}
    assert not assignment_eligibility(doc)[0]


def test_commercial_identity_and_missing_fields_are_blocked():
    doc = base_doc(); doc["company_name"] = "Corredora del Maule"
    assert "commercial_identity_or_profile" in assignment_eligibility(doc)[1]
    doc = base_doc(); doc["description"] = ""
    assert "missing_essential_fields" in assignment_eligibility(doc)[1]


def test_confirmed_profile_is_blocked_even_if_state_is_uncertain():
    doc = base_doc()
    doc["publisher_profile_context"] = {"confirmed_broker_count": 2, "commercial_identity_confirmed": True}
    assert not assignment_eligibility(doc)[0]
