from classifier_rules import detect_explicit_owner


def test_first_person_owner_phrases_are_strong():
    assert detect_explicit_owner({"description": "Soy dueño y arriendo mi departamento"})


def test_third_person_owner_phrases_are_not_strong():
    for phrase in ("trato directo con sus dueños", "vendida directamente por sus dueños",
                   "propiedad de un solo dueño", "documentación de los propietarios"):
        assert detect_explicit_owner({"description": phrase}) == []
