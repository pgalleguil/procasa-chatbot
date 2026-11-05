from datetime import datetime, timezone

from scrapers.scraper_yapo.extractor import extract_yapo_listing_images
from scrapers.scraper_yapo.normalize_yapo_519 import (
    _classification_updates,
    _materialized_updates,
    _values_equal,
)


def test_photo_extractor_keeps_only_unique_assets_for_exact_listing_id():
    html = """
    <html><body>
      <img src="https://photos.encuentra24.com/cl/33/21/89/33/33218933_a1">
      <img data-src="https://photos.encuentra24.com/cl/33/21/89/33/33218933_a1">
      <img src="https://photos.encuentra24.com/cl/33/21/89/33/33218933_b2">
      <img src="https://photos.encuentra24.com/cl/33/21/89/32/33218932_related">
      <img src="https://cdnjs.cloudflare.com/flags/cl.svg">
      <img src="https://photos.encuentra24.com/cl/33/21/89/33/332189330_wrong-boundary">
    </body></html>
    """
    images = extract_yapo_listing_images(html, "33218933", "https://www.yapo.cl/anuncio/33218933")
    assert images == [
        "https://photos.encuentra24.com/cl/33/21/89/33/33218933_a1",
        "https://photos.encuentra24.com/cl/33/21/89/33/33218933_b2",
    ]


def test_v3_broker_evidence_resolves_only_the_uncertain_canonical_final():
    document = {
        "listing_id": "28073704",
        "classification": {
            "state": "INCIERTO",
            "final_state": "INCIERTO",
            "final": "UNCERTAIN",
            "identity_classification": "UNCERTAIN",
            "reason": "Sin evidencia suficiente.",
            "evidence": [],
            "assignment_ready": False,
            "exclude_from_assignment": True,
            "owner_probability": None,
        },
        "classification_v3": {
            "state": "CORREDOR_PROBABLE",
            "reason": "Comisión explícita; corredor probable.",
            "evidence": ["honorarios de Corretaje"],
            "broker_evidence": [{"rule_id": "BROKERAGE_FEE_CHARGED"}],
            "classification_rule_ids": ["BROKERAGE_FEE_CHARGED"],
            "classifier_version": "yapo-deterministic-v7",
        },
    }
    updates, decision = _classification_updates(document)
    assert decision["new_state"] == "CORREDOR_PROBABLE"
    assert updates["classification.final"] == "BROKER_PROBABLE"
    assert updates["classification.state"] == "CORREDOR_PROBABLE"
    assert "honorarios de Corretaje" in updates["classification.evidence"]
    assert updates["classification.reason"] == "Comisión explícita; corredor probable."
    assert "classification.assignment_ready" not in updates
    assert "classification.exclude_from_assignment" not in updates
    assert "classification.owner_probability" not in updates


def test_professional_hard_veto_preserves_existing_confirmed_final_over_v3_probable():
    document = {
        "listing_id": "33092071",
        "classification": {
            "state": "INCIERTO",
            "final_state": "INCIERTO",
            "final": "BROKER_CONFIRMED",
            "identity_classification": "BROKER_CONFIRMED",
            "hard_veto": "PROFESSIONAL",
            "hard_broker_veto": True,
            "evidence": ["propiedades"],
            "assignment_ready": False,
            "exclude_from_assignment": True,
        },
        "classification_v3": {
            "state": "CORREDOR_PROBABLE",
            "reason": "Tipo de anunciante Agente del aviso; corredor probable.",
            "evidence": ["Agente", "propiedades"],
        },
    }
    updates, decision = _classification_updates(document)
    assert decision["new_state"] == "CORREDOR_SEGURO"
    assert decision["professional_override"] is True
    assert "classification.final" not in updates
    assert updates["classification.final_state"] == "CORREDOR_SEGURO"
    assert "veto profesional" in updates["classification.reason"]
    assert "classification.assignment_ready" not in updates
    assert "classification.exclude_from_assignment" not in updates
    assert "classification.owner_probability" not in updates


def test_new_live_owner_probable_gets_canonical_fields_without_assignment_fields():
    document = {
        "listing_id": "25939035",
        "classification": {
            "state": "INCIERTO",
            "evidence": ["Propietario"],
            "reason": "El publicador se declara propietario.",
            "assignment_ready": False,
            "exclude_from_assignment": True,
        },
        "classification_v3": {
            "state": "DUEÑO_PROBABLE",
            "reason": "Declaración atribuible de propiedad.",
            "evidence": ["atte su dueño"],
        },
    }
    updates, decision = _classification_updates(document)
    assert decision["new_state"] == "DUEÑO_PROBABLE"
    assert updates["classification.final"] == "OWNER_PROBABLE"
    assert updates["classification.identity_classification"] == "OWNER_PROBABLE"
    assert updates["classification.classification_state"] == "DUEÑO_PROBABLE"
    assert "classification.assignment_ready" not in updates
    assert "classification.exclude_from_assignment" not in updates
    assert "classification.owner_probability" not in updates


def test_materializer_uses_existing_shared_fields_and_does_not_create_owner_probability():
    document = {
        "comuna": "Maipu",
        "precio_uf": 3200.0,
        "precio_clp": 131000000.0,
        "precio_uf_normalizado": 3200.0,
        "precio_clp_normalizado": 131000000.0,
        "classification": {"assignment_ready": False, "owner_probability": None},
    }
    updates, preserved = _materialized_updates(document)
    assert "captacion_priority" in updates
    assert updates["captacion_comuna_sort"] == "maipu"
    assert "captacion_probability_sort" in updates
    assert "captacion_sort_date" not in updates
    assert "precio_uf_normalizado" not in updates
    assert "precio_uf_normalizado" in preserved
    assert not any(path.endswith("owner_probability") for path in updates)


def test_bson_date_readback_compares_at_millisecond_precision():
    expected = datetime(2026, 11, 4, 18, 12, 5, 987654, tzinfo=timezone.utc)
    stored = datetime(2026, 11, 4, 18, 12, 5, 987000)
    assert _values_equal(expected, stored)
    assert not _values_equal(expected, datetime(2026, 11, 4, 18, 12, 5, 986000))
