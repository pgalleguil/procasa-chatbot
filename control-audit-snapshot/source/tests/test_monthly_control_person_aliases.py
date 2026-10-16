from analytics.leads_queries import _control_person_aliases, _control_person_key


def test_paula_selector_matches_verified_prop360_full_name():
    assert _control_person_key("Executive B") == _control_person_key("Executive B")
    assert _control_person_aliases("Executive B") == [
        "Executive B", "Executive B",
    ]
