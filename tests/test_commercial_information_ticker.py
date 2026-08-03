import json
from pathlib import Path

from analytics.leads_service import _load_commercial_macro_information


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "templates" / "analytics" / "commercial_dashboard.html"
CSS = ROOT / "static" / "css" / "commercial_dashboard_v2.css"


def test_ticker_is_at_dashboard_end_and_has_three_configured_indicators():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'class="commercial-mobile-marquee"' in html
    assert html.index('class="commercial-mobile-marquee"') < html.index('id="kpiRow"')
    macro = json.loads((ROOT / "config" / "commercial_macro.json").read_text(encoding="utf-8"))
    assert set(macro["indicators"]) == {"uf", "usd", "tpm"}
    assert all({"value", "as_of", "source", "available"} <= set(item) for item in macro["indicators"].values())


def test_ticker_does_not_embed_macro_values_and_handles_missing_data():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "37.850" not in html and "945" not in html and "5,75" not in html
    macro = _load_commercial_macro_information()
    assert all(item["available"] is False and item["value"] is None for item in macro["indicators"].values())
    assert "commercialMobileMarquee" in html
    assert "commercial-information-ticker" not in html


def test_ticker_motion_accessibility_and_responsive_contract():
    html = TEMPLATE.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert 'commercial-mobile-marquee__group" aria-hidden="true"' in html
    marquee_js=html[html.index("function renderCommercialMobileMarquee"):html.index("function rPriorities")]
    assert "mouseenter" not in marquee_js and "focusin" not in marquee_js and "visibilitychange" not in marquee_js
    assert "prefers-reduced-motion: reduce" in css
    assert "commercial-mobile-marquee-scroll" in css
    assert "commercial-information-ticker" not in css
    assert "overflow-x: auto" in css


def test_executive_cards_use_explicit_microcopy_and_balanced_layout():
    html = TEMPLATE.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    for text in ("Avance de meta prorrateada", "Pace proyectado", "Conversión a visita", "Comisión neta estimada", "gestionados dentro", "Con actividad sin resultado", "Reconciliación"):
        assert text in html
    assert "repeat(6, minmax(0, 1fr))" in css
    assert "nth-child(5) { grid-column: 1 / -1; }" in css
    assert "valuation" in html or "valuation" in css or "valuation" in (ROOT / "analytics" / "leads_service.py").read_text(encoding="utf-8")


def test_card_one_hierarchy_card_four_empty_state_and_desktop_marquee():
    html = TEMPLATE.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert "cd-kpi-demand-pace" in html
    assert "grid-template-rows: auto auto auto minmax(48px, auto) auto" in css
    assert "cd-kpi-pace-badge" in html and "cd-kpi-foot" in html
    assert "NO EVALUABLE" in html and "Cobertura insuficiente" in html
    assert "No existen casos con trazabilidad suficiente" in html
    assert "animation: commercial-mobile-marquee-scroll 42s linear infinite" in css
    assert "animation: commercial-mobile-marquee-scroll 28s linear infinite" in css
    assert "position: fixed" not in css and "position: sticky" not in css
