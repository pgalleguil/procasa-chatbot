import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("yapo_extractor", ROOT / "scraper" / "extractor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_yapo_layout_extracts_price_description_and_professional_identity():
    html = """
    <h2 class="d3-property-details__title">Parcela en Curicó</h2>
    <div class="d3-property-info__price">$80.000.000</div>
    <div class="d3-property-about__text">Descripción completa de la propiedad y coordina tu visita.</div>
    <div class="contact_logo"><img alt="Rocamorapropiedades"></div>
    <div class="contact_info"><a class="contact_name" href="/user/profile/id/13726570">rocamorapropiedades</a><img title="Profesional"></div>
    """
    result = _load().extract_listing_fields(html, "https://www.yapo.cl/x/29922547")
    assert result["price"] == "$80.000.000"
    assert result["descripcion_source"] == "d3_property_about_text"
    assert result["publicador_visible"] == "rocamorapropiedades"
    assert result["seller_type"] == "PROFESIONAL"
    assert result["seller_profile_id"] == "13726570"
