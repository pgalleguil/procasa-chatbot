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
            "state": "DUEÑO_PROBABLE",
            "final": "OWNER_PROBABLE",
            "confidence": 0.89,
            "owner_probability": 0.89,
            "source": "toctoc_id_type",
            "decision_source": "toctoc_id_type",
            "reason": "TOCTOC_IDTYPE_1_OWNER_CANDIDATE",
            "assignment_ready": True,
            "pipeline_complete": True,
        },
    }
    value.update(overrides)
    return value


def _persistable(raw):
    document = build_crm_document(raw)
    apply_owner_probability_to_document(document)
    return document, validate_property_for_canonical_insert(document)


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


def test_id_type_one_owner_remains_assignable_after_schema_and_probability_pass():
    document, errors = _persistable(_raw("9999902"))
    assert errors == []
    assert document["classification"]["final"] == "OWNER_PROBABLE"
    assert document["classification"]["state"] == "DUEÑO_PROBABLE"
    assert document["classification"]["assignment_ready"] is True
    assert document["listing_status"] == "ACTIVE"


def test_removed_listing_does_not_replace_owner_identity():
    document, errors = _persistable(_raw(
        "9999903",
        html_validation_status="LISTING_REMOVED",
        html_validation_reason="Anuncio eliminado por el anunciante",
        processing_status="AD_REMOVED",
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
