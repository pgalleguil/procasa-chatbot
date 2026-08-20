import sys

sys.path.insert(0, r"C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok_main_normalization")

from comuna_utils import normalize_toctoc_commune
from crm_schema import build_crm_document


def test_toctoc_structured_santiago_id_maps_to_crm_alias():
    assert normalize_toctoc_commune(
        "Santiago", commune_id=339, structured_label="Santiago", structured=True
    ) == "santiago-centro"


def test_toctoc_structured_providencia_stays_providencia():
    assert normalize_toctoc_commune(
        "Santiago", commune_id=131, structured_label="Providencia", structured=True
    ) == "providencia"


def test_toctoc_structured_nunoa_stays_nunoa():
    assert normalize_toctoc_commune(
        "Ñuñoa", commune_id=118, structured_label="Ñuñoa", structured=True
    ) == "nunoa"


def test_structured_providencia_wins_over_text_santiago():
    assert normalize_toctoc_commune(
        "Santiago", structured_label="Providencia", structured=True
    ) == "providencia"


def test_generic_santiago_text_is_not_forced_to_santiago_centro():
    assert normalize_toctoc_commune("Santiago") == "santiago"


def test_crm_schema_uses_structured_toctoc_commune_id():
    raw = {
        "listing_id": "1",
        "url": "https://www.toctoc.com/1",
        "title": "Aviso",
        "operacion": "venta",
        "tipo_propiedad": "departamento",
        "comuna": "Santiago",
        "comuna_id": 339,
        "comuna_structured_label": "Santiago",
        "comuna_structured": True,
        "comuna_evidence_source": "toctoc_bff",
        "description": "Aviso de prueba",
        "classification": {"state": "INCIERTO", "confidence": 0.55},
    }
    assert build_crm_document(raw)["comuna_slug"] == "santiago-centro"


def test_crm_schema_does_not_force_unstructured_santiago_text():
    raw = {
        "listing_id": "2",
        "url": "https://www.toctoc.com/2",
        "title": "Aviso en Santiago",
        "operacion": "venta",
        "tipo_propiedad": "departamento",
        "comuna": "Santiago",
        "description": "La ciudad de Santiago",
        "classification": {"state": "INCIERTO", "confidence": 0.55},
    }
    assert build_crm_document(raw)["comuna_slug"] == "santiago"
