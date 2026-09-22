from __future__ import annotations

import sys
import importlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_root_config = importlib.import_module("config")
sys.modules.pop("config", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scrapers" / "scraper_toctoc"))
from crm_schema import build_crm_document
from mongo_store import validate_property_for_canonical_insert
from downloader import validate_html
from enrich import _enrich_property_fields
sys.path.pop(0)
sys.modules["config"] = _root_config
sys.modules.pop("crm_schema", None)
sys.modules.pop("mongo_store", None)
from owner_probability import apply_owner_probability_to_document


def _raw(listing_id: str, **overrides):
    value = {
        "listing_id": listing_id,
        "url": f"https://www.toctoc.com/propiedades/{listing_id}",
        "title": "Casa familiar publicada",
        "description": "Descripción completa de una propiedad publicada para prueba.",
        "operation": "Venta Usado Particular",
        "tipo_propiedad": "casa",
        "comuna": "Maipu",
        "region": "Metropolitana",
        "publicador_visible": "Particular",
        "seller_type": "PARTICULAR",
        "seller_type_evidence": "idType=1",
        "seller_id_type_raw": "1",
        "seller_profile_id": "profile-1",
        "seller_client_id": "client-1",
        "classification": {
            "state": "INCIERTO",
            "final": "UNCERTAIN",
            "confidence": 0.5,
            "source": "classification_service",
            "reason": "INCONCLUSIVE",
            "assignment_ready": False,
            "pipeline_complete": True,
        },
    }
    value.update(overrides)
    return value


def _persistable(raw):
    document = build_crm_document(raw)
    apply_owner_probability_to_document(document)
    return document, validate_property_for_canonical_insert(document)


def _explicit_owner_classification():
    return {
        "state": "DUEÑO_PROBABLE",
        "final": "OWNER_PROBABLE",
        "confidence": 0.82,
        "canonical_confidence": 0.82,
        "owner_probability": 0.82,
        "source": "structural_rules",
        "decision_source": "structural_rules",
        "reason": "EXPLICIT_OWNER_DIRECT_SALE",
        "assignment_ready": True,
        "pipeline_complete": True,
    }


def test_structural_broker_uses_operation_fallback_and_serializes_canonically():
    document, errors = _persistable(_raw(
        "9999901",
        operation="Venta Usado Corredor",
        publicador_visible="Broker Comercial",
        seller_type="EMPRESA",
        seller_type_evidence="Venta Usado Corredor",
        seller_id_type_raw="2",
        classification={
            "state": "CORREDOR_SEGURO",
            "final": "BROKER_CONFIRMED",
            "confidence": 0.0,
            "source": "structural_rules",
            "decision_source": "structural_rules",
            "hard_veto": "PROFESSIONAL",
            "hard_broker_signal": True,
            "reason": "TOCTOC_IDTYPE_2_BROKER",
        },
    ))
    assert errors == []
    assert document["operacion"] == "venta usado corredor"
    assert document["seller_id_type_raw"] == "2"
    assert document["classification"]["final"] == "BROKER_CONFIRMED"
    assert document["classification"]["assignment_ready"] is False


def test_id_type_one_alone_remains_uncertain_after_schema_and_probability_pass():
    document, errors = _persistable(_raw("9999902"))
    assert errors == []
    assert document["classification"]["final"] == "UNCERTAIN"
    assert document["classification"]["state"] == "INCIERTO"
    assert document["classification"]["assignment_ready"] is False
    assert document["listing_status"] == "ACTIVE"


def test_stale_idtype_one_owner_promotion_is_not_preserved_by_canonicalization():
    raw = _raw(
        "9999914",
        classification={
            "state": "DUEÑO_PROBABLE",
            "final": "OWNER_PROBABLE",
            "confidence": 0.89,
            "owner_probability": 0.89,
            "source": "toctoc_id_type",
            "reason": "TOCTOC_IDTYPE_1_OWNER_CANDIDATE",
            "assignment_ready": True,
            "pipeline_complete": True,
        },
    )
    document, errors = _persistable(raw)
    assert errors == []
    assert document["classification"]["final"] == "UNCERTAIN"
    assert document["classification"]["state"] == "INCIERTO"
    assert document["classification"]["assignment_ready"] is False


def test_removed_listing_does_not_replace_owner_identity():
    document, errors = _persistable(_raw(
        "9999903",
        html_validation_status="LISTING_REMOVED",
        html_validation_reason="Anuncio eliminado por el anunciante",
        processing_status="AD_REMOVED",
        classification=_explicit_owner_classification(),
    ))
    assert errors == []
    assert document["classification"]["final"] == "OWNER_PROBABLE"
    assert document["classification"]["identity_classification"] == "OWNER_PROBABLE"
    assert document["classification"]["listing_status"] == "REMOVED"
    assert document["classification"]["assignment_ready"] is False
    assert document["classification"]["assignment_block_reasons"] == ["LISTING_REMOVED"]


def test_structural_broker_and_owner_never_become_assignable_from_listing_status():
    broker, broker_errors = _persistable(_raw(
        "9999904",
        operation="Venta Usado Corredor",
        seller_type="EMPRESA",
        seller_id_type_raw="2",
        html_validation_status="LISTING_REMOVED",
        processing_status="AD_REMOVED",
        classification={
            "state": "CORREDOR_SEGURO",
            "final": "BROKER_CONFIRMED",
            "confidence": 0.0,
            "source": "structural_rules",
            "hard_veto": "PROFESSIONAL",
        },
    ))
    assert broker_errors == []
    assert broker["classification"]["final"] == "BROKER_CONFIRMED"
    assert broker["classification"]["assignment_ready"] is False


def test_http_error_is_not_reported_as_removed_listing():
    result = validate_html(
        "<html><head><title>403 Forbidden</title></head><body><h1>403 Forbidden</h1></body></html>",
        status_code=403,
    )
    assert result["status"] == "HTTP_ERROR"


def test_missing_detail_markers_are_html_change_not_removed_proof():
    result = validate_html("<html><body><div>" + ("New frontend shell " * 16) + "</div></body></html>")
    assert result["status"] == "HTML_CHANGED"


def test_explicit_owner_classification_survives_lower_heuristic_probability():
    raw = _raw("9999910")
    raw["classification"] = _explicit_owner_classification()
    raw["classification"].update({"owner_probability": 0.15, "confidence": 0.15})
    document, errors = _persistable(raw)
    assert errors == []
    assert document["classification"]["final"] == "OWNER_PROBABLE"
    assert document["classification"]["state"] == "DUEÑO_PROBABLE"
    assert document["classification"]["assignment_ready"] is True


def test_canonical_identity_conflict_stays_uncertain_and_not_assignable():
    raw = _raw(
        "9999913",
        classification={
            "state": "INCIERTO",
            "final": "UNCERTAIN",
            "confidence": 0.6,
            "source": "evidence_precedence",
            "classification_conflict": True,
            "conflict_state": "IDENTITY_CONFLICT",
            "reason": "IDENTITY_CONFLICT",
        },
    )
    document, errors = _persistable(raw)
    assert errors == []
    assert document["classification"]["final"] == "UNCERTAIN"
    assert document["classification"]["state"] == "INCIERTO"
    assert document["classification"]["assignment_ready"] is False
    assert document["classification"]["classification_conflict"] is True


def test_structural_publisher_hard_veto_overrides_idtype_owner_and_score():
    raw = _raw(
        "9999911",
        publicador_visible="Bissac Corredores de propiedades",
        classification={
            "state": "DUEÑO_PROBABLE",
            "final": "OWNER_PROBABLE",
            "owner_probability": 0.89,
            "confidence": 0.89,
            "source": "toctoc_id_type",
        },
    )
    document, errors = _persistable(raw)
    assert errors == []
    assert document["classification"]["final"] == "BROKER_CONFIRMED"
    assert document["classification"]["state"] == "CORREDOR_SEGURO"
    assert document["classification"]["hard_veto"] == "PROFESSIONAL"
    assert document["classification"]["assignment_ready"] is False


def test_boolean_professional_hard_veto_is_normalized_as_broker():
    raw = _raw(
        "9999912",
        classification={
            "state": "INCIERTO",
            "final": "UNCERTAIN",
            "professional_hard_veto": True,
            "source": "structural_rules",
        },
    )
    document, errors = _persistable(raw)
    assert errors == []
    assert document["classification"]["final"] == "BROKER_CONFIRMED"
    assert document["classification"]["state"] == "CORREDOR_SEGURO"
    assert document["classification"]["assignment_ready"] is False


def test_detail_price_wins_and_card_price_is_auditable():
    enriched = _enrich_property_fields(
        {"price_uf": "UF 11000", "price_clp": "$ 450909250"},
        "https://www.toctoc.com/propiedades/compraparticularsr/parcela/maipu/test/4199720",
        40842.07,
        "2026-09-22",
    )
    assert enriched["price_source"] == "DETAIL"
    assert enriched["detail_price_clp"] == 450909250
    assert enriched["precio_clp"] == 450909250
    crm = build_crm_document({
        **_raw("4199720"),
        **enriched,
        "card_price_raw": "$449.292.690",
        "card_currency": "CLP",
        "card_price_clp": 449292690,
    })
    assert crm["canonical_price_source"] == "DETAIL"
    assert crm["precio_clp"] == 450909250
    assert crm["card_price_clp"] == 449292690
    assert crm["price_difference"] == 1616560


def test_agricultural_field_type_is_recovered_from_exact_route_slug():
    enriched = _enrich_property_fields(
        {},
        "https://www.toctoc.com/propiedades/compraparticularsr/campoagricola/isla-de-maipo/test/4254711",
        40844.79,
        "2026-09-22",
    )
    assert enriched["tipo_propiedad"] == "agricola"
