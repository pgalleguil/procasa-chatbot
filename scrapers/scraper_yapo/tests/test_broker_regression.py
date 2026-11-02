"""Regression: brokers (RE/MAX 32589746) must never be assignable to the team.

The pipeline once silently lost its broker rule sets (UTF-8 BOM in the JSON
under data/ made every set load as []) and the owner-probability gate
downgraded CORREDOR_SEGURO -> INCIERTO, so the RE/MAX listing became
assignment_ready. These tests pin down both fixes.
"""
import sys
from pathlib import Path

# Keep the Yapo package and root CRM modules under distinct import names.
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

import owner_probability  # noqa: E402
from captacion_assignment_eligibility import assignment_eligibility  # noqa: E402
from scrapers.scraper_yapo.classifier_rules import (  # noqa: E402
    classify_obvious_broker,
    validate_classification_rules,
)


def test_rule_sets_load_non_empty():
    # BOM regression: previously every rule set loaded as [] silently.
    empty = validate_classification_rules()
    assert empty == [], f"rule sets cargaron vacios: {empty}"


def test_proube_listings_32115174_32114915_are_broker():
    # Logo "Info PROurbe" (users/logo/.../17462647) that was missed because
    # the brand was absent from known_broker_brands.json.
    for logo in ("Info PROurbe", "PROurbe"):
        extracted = {
            "contact_logo_alt": logo,
            "contact_name": "Maria del Pilar Rojas",
            "publicador_visible": "Maria del Pilar Rojas",
        }
        result = classify_obvious_broker(extracted)
        assert result is not None, f"logo {logo!r} no detectado como corredor"
        assert result["state"] == "CORREDOR_SEGURO"
        assert result["source"] in {"rules_json", "structural_rules"}


def test_remax_listing_32589746_is_broker():
    # Fields extracted from html_dumps/.../3747c01c3f04987accceea41b2010a67.html
    # (contact_logo_alt='RE MAX - CENTRAL', vendedor Carla Pollastri).
    extracted = {
        "title": "Excelente oportunidad casa de 3d y 1b",
        "contact_logo_alt": "RE MAX - CENTRAL",
        "contact_name": "Carla Pollastri",
        "seller_type": "profesional",
        "seller_name": "Carla Pollastri",
    }
    result = classify_obvious_broker(extracted)
    assert result is not None
    assert result["state"] == "CORREDOR_SEGURO"
    assert result["source"] in {"rules_json", "structural_rules"}
    assert any("remax" in e or "re max" in e for e in result["evidence"])


def _remax_doc():
    return {
        "listing_id": "32589746",
        "url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/excelente-oportunidad-casa-de-3d-y-1b/32589746",
        "origen": "yapo",
        "comuna": "La Florida",
        "description": "Casa en venta, location Santiago.",
        "contact_logo_alt": "RE MAX - CENTRAL",
        "contact_name": "Carla Pollastri",
        "seller_name": "Carla Pollastri",
        "scrape_stage": "classification_done",
        "html_validation_status": "OK",
        "classification": {
            "state": "CORREDOR_SEGURO",
            "source": "rules_json",
            "confidence": 0.99,
            "evidence": ["contact_logo_alt:remax"],
        },
    }


def test_owner_probability_gate_keeps_corredor_not_assignable():
    doc = _remax_doc()
    owner_probability.apply_owner_probability_to_document(doc)
    cls = doc["classification"]
    assert cls["state"] == "CORREDOR_SEGURO"
    assert cls["assignment_ready"] is False
    assert cls["exclude_from_assignment"] is True
    assert "PROFESSIONAL_HARD_VETO" in (cls.get("assignment_block_reasons") or [])
    eligible, _ = assignment_eligibility(doc)
    assert eligible is False
