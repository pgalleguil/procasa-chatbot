"""Offline contract: no embeddings/LLM/API, only curated review attributes."""
from datetime import datetime, timezone

import pytest

from chatbot.executive_whatsapp_semantic import build_semantic_candidate


REVIEWED = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)


def result(**overrides):
    value = {
        "review_status": "APPROVED_HUMAN_REVIEW",
        "opt_out": False,
        "commercial_status": "ACTIVE_INTEREST",
        "evidence_provider_keys": ["wasender:verified-message-1"],
    }
    value.update(overrides)
    return value


def prefs(**overrides):
    data = {
        "operacion": "venta",
        "tipo_propiedad": "departamento",
        "comunas": ["Ñuñoa", "Providencia", "Maipú"],
        "presupuesto_max_uf": 4300,
        "dormitorios_min": 2,
        "caracteristicas": ["terraza", "estacionamiento"],
    }
    data.update(overrides)
    return data


def candidate(audit=None, preferences=None):
    return build_semantic_candidate(
        lead_id="L1",
        audit_result=result() if audit is None else audit,
        reviewed_preferences=prefs() if preferences is None else preferences,
        reviewed_at=REVIEWED,
    )


def test_curated_profile_has_no_raw_conversation_or_phone():
    a = candidate()
    assert a is not None
    assert a["embedding_generated"] is False
    assert a["index_write_performed"] is False
    assert "nunoa" in a["vector_text"]
    assert "4300" in a["vector_text"]
    assert a["lead_id"] == "L1"
    assert "wasender:verified-message-1" not in a["vector_text"]
    assert "L1" not in a["vector_text"]


@pytest.mark.parametrize("audit", [
    result(review_status="PENDING_HUMAN_REVIEW"),
    result(opt_out=True),
    result(opt_out=None),
    result(commercial_status="NO_LONGER_SEARCHING"),
    result(commercial_status="TRANSACTION_COMPLETED"),
    result(commercial_status="UNKNOWN"),
    result(evidence_provider_keys=[]),
])
def test_unreviewed_optout_closed_or_unevidenced_lead_excluded(audit):
    assert candidate(audit=audit) is None


def test_untrusted_free_text_is_not_vectorized():
    injected = prefs(
        comunas=["Pablo Galleguillos", "Av. Siempre Viva 123", "San Miguel"],
        caracteristicas=["Mi número es +56912345678", "piscina"],
        tipo_propiedad="cliente@gmail.com",
        operacion="Contactarme en +56912345678",
    )
    a = candidate(preferences=injected)
    assert a is not None
    text = a["vector_text"]
    for private in ("pablo", "123", "56912345678", "@", "contactarme"):
        assert private not in text
    assert "san miguel" in text
    assert "piscina" in text


def test_profile_stability_and_revision_changes_on_preference_change():
    a = candidate()
    b = candidate()
    c = candidate(preferences=prefs(presupuesto_max_uf=4500))
    assert a["revision_sha256"] == b["revision_sha256"]
    assert a["revision_sha256"] != c["revision_sha256"]


def test_no_useful_preference_yields_no_embedding():
    assert candidate(preferences={
        "comunas": [], "caracteristicas": [],
        "presupuesto_max_uf": None,
        "tipo_propiedad": "desconocido",
        "operacion": "desconocido",
    }) is None


def test_rejects_incomplete_review_or_naive_clock():
    with pytest.raises(ValueError, match="structured_review_required"):
        candidate(preferences="Departamento San Miguel")
    with pytest.raises(ValueError, match="reviewed_at_requires_timezone"):
        build_semantic_candidate(
            lead_id="L1", audit_result=result(),
            reviewed_preferences=prefs(), reviewed_at=datetime(2026, 11, 11),
        )
