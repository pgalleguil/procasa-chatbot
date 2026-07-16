from captacion_kpis import (
    AVAILABLE_STATES,
    MANAGEMENT_STATES,
    VISIBLE_CLASSIFICATION_STATES,
    build_kpi_queries,
)


def test_probable_owner_is_visible_to_cards():
    assert "DUEÑO_PROBABLE" in VISIBLE_CLASSIFICATION_STATES


def test_management_includes_every_followup_response():
    assert {
        "Por contactar",
        "Contacto exitoso",
        "Sin respuesta",
        "Reunión agendada",
    }.issubset(MANAGEMENT_STATES)


def test_available_and_management_are_separate_response_groups():
    queries = build_kpi_queries({"origen": {"$in": ["yapo", "toctoc"]}})

    assert queries["available"]["gestion.estado"]["$in"] == list(AVAILABLE_STATES)
    assert "Por contactar" not in queries["available"]["gestion.estado"]["$in"]
    assert "Por contactar" in queries["management"]["gestion.estado"]["$in"]


def test_kpi_queries_preserve_assignment_scope():
    assignment = {"gestion.ejecutivo_asignado": "Paula Morales"}
    queries = build_kpi_queries(assignment)

    assert all(query["gestion.ejecutivo_asignado"] == "Paula Morales" for query in queries.values())
