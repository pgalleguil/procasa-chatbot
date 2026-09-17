from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scrapers" / "scraper_toctoc"))

from broker_identity import detect_hard_broker_signal

# The scraper's legacy modules import their sibling ``config`` unqualified,
# while the CRM has a different root-level module with the same name.
_root_config = sys.modules.pop("config", None)
from scrapers.scraper_toctoc.extractor import extract_listing_fields
if _root_config is not None:
    sys.modules["config"] = _root_config


def _html(*, id_type=2, logo="", operation="Venta Usado", profile_url=""):
    payload = {
        "props": {
            "pageProps": {
                "initialState": {
                    "property": {
                        "property": {
                            "data": {
                                "idProperty": 4351894,
                                "title": "Publicación de prueba",
                                "description": "Descripción completa de prueba.",
                                "address": {"commune": "Maipu", "region": "Metropolitana"},
                                "client": {
                                    "id": 9988,
                                    "idType": id_type,
                                    "name": "Publicador de prueba",
                                    "logo": logo,
                                    "url": profile_url,
                                },
                                "operation": {"operation": operation},
                            }
                        }
                    }
                }
            }
        }
    }
    return '<html><body><h1>Prueba</h1><script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script></body></html>"


def test_seller_type_is_derived_from_id_type_without_contact_dom():
    result = extract_listing_fields(_html(id_type=2), "https://www.toctoc.com/venta/casa/maipu/4351894")
    assert result["seller_type"] == "CORREDOR"
    assert result["seller_type_source"] == "detail_next_data.client.idType"
    assert result["seller_id_type"] == "2"


def test_id_type_particular_is_preserved_as_particular():
    result = extract_listing_fields(_html(id_type=1, operation="Venta Usado Particular"), "https://www.toctoc.com/venta/casa/maipu/4351894")
    assert result["seller_type"] == "PARTICULAR"
    assert result["seller_type_evidence"] == "idType=1"


def test_corredora_profile_is_structural_evidence_even_without_seller_type():
    document = {
        "seller_type": "",
        "seller_profile_logo": "https://cdn.toctoc.com/logos/corredora/93848/logo.png",
        "structural_signals": {"profile_path_signal": True},
    }
    signal = detect_hard_broker_signal(document, extracted=document)
    assert signal is not None
    assert signal["reason_code"] in {"BROKER_PROFILE_PATH", "COMMERCIAL_BROKER_TERM"}


def test_operation_corredor_is_structural_evidence():
    result = extract_listing_fields(
        _html(id_type=2, logo="", operation="Venta Usado Corredor"),
        "https://www.toctoc.com/venta/casa/maipu/4351894",
    )
    assert result["operation_label_raw"] == "Venta Usado Corredor"
    assert result["structural_signals"]["operation_broker_signal"] is True
    assert detect_hard_broker_signal(result, extracted=result) is not None


def test_extractor_does_not_require_seller_type_for_hard_operation_signal():
    document = {
        "seller_type": "",
        "operation_label_raw": "Venta Usado Corredor",
        "structural_signals": {"operation_broker_signal": True},
    }
    assert detect_hard_broker_signal(document, extracted=document)["reason_code"] == "BROKER_OPERATION_LABEL"
