import json
from pathlib import Path

from analytics.leads_service import _load_commercial_macro_information


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "templates" / "analytics" / "commercial_dashboard.html"
CSS = ROOT / "static" / "css" / "commercial_dashboard_v2.css"


def test_ticker_is_at_dashboard_end_and_has_three_configured_indicators():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert html.index('id="commercialInformationTicker"') < html.index('</main>')
    macro = json.loads((ROOT / "config" / "commercial_macro.json").read_text(encoding="utf-8"))
    assert set(macro["indicators"]) == {"uf", "usd", "tpm"}
    assert all({"value", "as_of", "source", "available"} <= set(item) for item in macro["indicators"].values())


def test_ticker_does_not_embed_macro_values_and_handles_missing_data():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "37.850" not in html and "945" not in html and "5,75" not in html
    macro = _load_commercial_macro_information()
    assert all(item["available"] is False and item["value"] is None for item in macro["indicators"].values())
    assert "No actualizado" in html
    assert "Indicadores macroeconómicos pendientes de sincronización" in html


def test_ticker_motion_accessibility_and_responsive_contract():
    html = TEMPLATE.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert 'aria-hidden="true"' in html
    assert "mouseenter" in html and "focusin" in html and "visibilitychange" in html
    assert "prefers-reduced-motion: reduce" in css
    assert "animation-play-state: paused" in css
    assert "position: fixed" not in css[css.index(".commercial-information-ticker"):]
    assert "overflow-x: auto" in css


def test_executive_cards_use_explicit_microcopy_and_balanced_layout():
    html = TEMPLATE.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    for text in ("Avance de meta prorrateada", "Pace proyectado", "Conversión a visita", "Comisión neta estimada", "gestionados dentro", "Con actividad sin resultado", "Reconciliación"):
        assert text in html
    assert "repeat(6, minmax(0, 1fr))" in css
    assert "nth-child(5) { grid-column: 1 / -1; }" in css
    assert "valuation" in html or "valuation" in css or "valuation" in (ROOT / "analytics" / "leads_service.py").read_text(encoding="utf-8")
