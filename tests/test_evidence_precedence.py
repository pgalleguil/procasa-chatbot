from types import SimpleNamespace

from captacion_assignment_eligibility import can_assign_property
from classification_service import classify_capture


HEALTHY = {"healthy": True, "degraded": False}


def _config():
    return SimpleNamespace(
        deepseek_enabled=False,
        deepseek_api_key="",
        deepseek_description_max_chars=6000,
        max_ai_calls_per_run=10,
        max_input_tokens_per_run=100000,
        max_output_tokens_per_run=20000,
        max_estimated_cost_per_run=5.0,
        deepseek_max_tokens=500,
    )


def _doc(*, id_type=None, title="Casa particular", description="Venta directa", **extra):
    doc = {
        "portal": "TOCTOC",
        "source_portal": "TOCTOC",
        "listing_id": "test-listing",
        "url": "https://www.toctoc.com/test-listing",
        "comuna": "Maipu",
        "title": title,
        "description": description,
        "publicador_visible": "Juan Perez",
        "seller_type": "PARTICULAR",
    }
    if id_type is not None:
        doc["seller_id_type_raw"] = str(id_type)
    doc.update(extra)
    return doc


def _classify(doc, **kwargs):
    return classify_capture(
        doc,
        config=_config(),
        health=HEALTHY,
        allow_real_ai=False,
        **kwargs,
    )["classification"]


def _gate(doc, classification, **context):
    return can_assign_property(
        {
            **doc,
            "classification": classification,
            "pipeline_complete": classification["pipeline_complete"],
            "pipeline_state": classification["pipeline_state"],
        },
        context,
    )


def test_idtype1_agenda_visit_is_owner_and_assignable():
    doc = _doc(id_type=1, description="Agenda tu visita y conoce la propiedad.")
    classification = _classify(doc)
    decision = _gate(doc, classification)
    assert classification["final"] == "OWNER_PROBABLE"
    assert classification["evidence_type"] == "EXPLICIT_OWNER_STRUCTURAL"
    assert classification["confidence"] == classification["owner_probability"]
    assert decision["assignment_ready"] is True


def test_idtype1_entrega_inmediata_is_owner_and_assignable():
    classification = _classify(_doc(id_type=1, description="Entrega inmediata."))
    assert classification["final"] == "OWNER_PROBABLE"
    assert classification["assignment_ready"] is True


def test_idtype1_multiple_weak_signals_never_become_broker():
    classification = _classify(
        _doc(
            id_type=1,
            description="Entrega inmediata. Agenda tu visita. Coordinar visita.",
        )
    )
    assert classification["final"] == "OWNER_PROBABLE"
    assert classification["evidence_strength"] == "EXPLICIT_OWNER_STRUCTURAL"


def test_idtype1_exact_registry_match_is_identity_conflict_and_blocked():
    doc = _doc(
        id_type=1,
        # Supplied lookup is read from the document when no DB is injected.
        broker_identity_match={
            "matched": True,
            "match_type": "EXACT_PROFILE_ID",
            "evidence": ["profile_id=confirmed-broker"],
        },
    )
    classification = _classify(doc)
    decision = _gate(doc, classification, broker_identity_match={"matched": True})
    assert classification["final"] == "UNCERTAIN"
    assert classification["classification_conflict"] is True
    assert classification["conflict_state"] == "IDENTITY_CONFLICT"
    assert classification["assignment_ready"] is False
    assert decision["assignment_ready"] is False
    assert "classification_conflict" in decision["assignment_block_reasons"]


def test_idtype1_strong_broker_text_is_identity_conflict_and_blocked():
    doc = _doc(id_type=1, description="Corredora de propiedades, sujeto a comisión.")
    classification = _classify(
        doc,
        classification_hint={
            "state": "CORREDOR_SEGURO",
            "evidence": ["corredora de propiedades"],
            "evidence_strength": "STRONG",
        },
        strong_text_broker=True,
    )
    assert classification["final"] == "UNCERTAIN"
    assert classification["conflict_state"] == "IDENTITY_CONFLICT"
    assert classification["assignment_ready"] is False


def test_idtype2_owner_language_is_still_broker():
    classification = _classify(
        _doc(id_type=2, title="Vende su dueño", description="Trato directo con dueño.")
    )
    assert classification["final"] == "BROKER_CONFIRMED"
    assert classification["evidence_type"] == "STRUCTURAL_BROKER"
    assert classification["assignment_ready"] is False


def test_idtype3_is_out_of_scope_and_blocked():
    classification = _classify(_doc(id_type=3, title="Proyecto nuevo"))
    assert classification["final"] == "OUT_OF_SCOPE_NEW_DEVELOPMENT"
    assert classification["assignment_ready"] is False
    assert classification["ai_eligible"] is False


def test_weak_corredor_probable_hint_cannot_escalate_to_strong_broker():
    classification = _classify(
        _doc(description="Entrega inmediata y agenda tu visita."),
        classification_hint={
            "state": "CORREDOR_PROBABLE",
            "evidence": ["agenda tu visita"],
            "evidence_strength": "WEAK",
        },
        strong_text_broker=True,
    )
    assert classification["final"] == "UNCERTAIN"
    assert classification["final"] != "BROKER_CONFIRMED"
    assert classification["pipeline_complete"] is True
    assert classification["assignment_ready"] is False
