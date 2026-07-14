import importlib.util
from pathlib import Path

from maintain_paula_portfolio import resolve_price_patch


ROOT = Path(__file__).resolve().parents[1]


def _load_crm_schema():
    spec = importlib.util.spec_from_file_location("toctoc_crm_schema", ROOT / "scraper_toctoc" / "crm_schema.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_valid_clp_produces_uf():
    patch = resolve_price_patch({"precio_clp": 580_000}, 40_844.79, "2026-07-14")
    assert patch["precio_uf"] == 14.2


def test_original_uf_is_never_overwritten():
    assert resolve_price_patch({"precio_uf": 1234.5, "precio_clp": 580_000}, 40_844.79, "2026-07-14") == {}


def test_zero_or_invalid_price_stays_missing():
    assert resolve_price_patch({"precio_clp": 0}, 40_844.79, "2026-07-14") == {}
    assert resolve_price_patch({"precio_raw": "consultar"}, 40_844.79, "2026-07-14") == {}


def test_toctoc_schema_converts_numeric_clp_and_preserves_original_uf():
    schema = _load_crm_schema()
    clp = schema.build_crm_document({"listing_id": "1", "precio_clp": 580000})
    assert clp["precio_clp"] == 580000
    assert clp["precio_uf"] == 14.2
    assert clp["precio_conversion_source"] == "calculated_from_clp"
    uf = schema.build_crm_document({"listing_id": "2", "precio_uf": 1234.5})
    assert uf["precio_uf"] == 1234.5
