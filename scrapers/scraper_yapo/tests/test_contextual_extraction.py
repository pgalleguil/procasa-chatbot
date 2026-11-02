from __future__ import annotations

from scrapers.scraper_yapo import extractor
from scrapers.scraper_yapo.extractor import extract_listing_fields


def test_title_is_not_truncated_and_source_is_recorded():
    title = "Casa luminosa " + ("con patio y excelente ubicación " * 8)
    result = extract_listing_fields(f"<html><h1>{title}</h1></html>")
    assert result["title"] == title.strip()
    assert result["title_original_len"] == len(title.strip())
    assert result["title_source"] == "h1"
    assert result["title_is_truncated"] is False


def test_fullest_description_node_is_used_without_meta_summary_substitution():
    full = "Descripción completa con detalles. " * 20
    html = f"""
    <html><head><meta property="og:description" content="Resumen corto"></head><body>
      <div class="description">Texto breve.</div>
      <div class="d3-property-about__text">{full}</div>
    </body></html>
    """
    result = extract_listing_fields(html)
    assert result["description"] == full.strip()
    assert result["description_summary"] == ""
    assert result["descripcion_source"] == "d3_property_about_text"
    assert any(item["source"] == "class_description" for item in result["description_sources"])


def test_truncation_markers_and_expand_controls_are_preserved():
    result = extract_listing_fields(
        '<html><div class="d3-property-about__text">Casa amplia... Ver más</div>'
        '<button aria-expanded="false">Mostrar más</button></html>'
    )
    assert result["descripcion_is_truncated"] is True
    assert result["description_truncation_reason"] == "portal_truncation_marker_or_expand_control"


def test_meta_description_is_diagnostic_summary_not_full_description():
    result = extract_listing_fields('<html><meta property="og:description" content="Resumen"></html>')
    assert result["description"] == ""
    assert result["description_summary"] == "Resumen"
    assert result["descripcion_source"] == "missing_full_description"
    assert result["descripcion_is_truncated"] is True


def test_no_beautifulsoup_fallback_keeps_entire_heading(monkeypatch):
    title = "Título completo " + ("repetido " * 25)
    monkeypatch.setattr(extractor, "BeautifulSoup", None)
    result = extract_listing_fields(f"<html><h1>{title}</h1><p>Texto íntegro.</p></html>")
    assert result["title"] == title.strip()
    assert len(result["title"]) > 120
    assert "Texto íntegro." in result["description"]
