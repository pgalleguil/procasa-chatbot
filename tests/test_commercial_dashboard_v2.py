import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "analytics" / "commercial_dashboard.html"
CSS = ROOT / "static" / "css" / "commercial_dashboard_v2.css"


def test_v2_visual_layer_and_executive_order_are_present():
    html = TEMPLATE.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert 'class="cd commercial-dashboard-v2"' in html
    assert 'href="/static/css/commercial_dashboard_v2.css"' in html
    assert "--commercial-brand-600: #0284c7" in css
    assert "--commercial-page: #f6f8fb" in css
    assert ".commercial-dashboard-v2" in css
    assert "function reorderExecutiveBlocks()" in html
    for value in ("$('kpiRow')", "$('priorityList')", "#tab-exec > .cd-grid.cd-g-exec", "#tab-exec > .commercial-v2-ops-grid", "$('executiveStory')", "$('funnel')?.closest('.cd-card')", "$('insights')"):
        assert value in html


def test_v2_preserves_dashboard_contracts_and_adds_required_states():
    html = TEMPLATE.read_text(encoding="utf-8")
    for value in ("Leads recibidos", "Hot actuales", "Intenci", "visita", "Cumplimiento SLA"):
        assert value in html
    for value in ('id="kpiRow"', 'id="evChart"', 'id="slaBody"', 'id="funnel"', 'id="filters"', 'id="tabNav"'):
        assert value in html
    assert html.count('id="managementTargets"') == 1
    assert "sanitizeUnavailableUi" in html
    assert "prefers-reduced-motion" in CSS.read_text(encoding="utf-8")
    assert "aria-expanded" in html
    assert "Cargando información comercial" in html
    assert "commercial-v2-skeleton" in CSS.read_text(encoding="utf-8")


def test_v2_css_uses_only_scoped_component_selectors():
    css = CSS.read_text(encoding="utf-8")
    selectors = [line.strip() for line in css.splitlines() if "{" in line and not line.lstrip().startswith("@")]
    assert selectors
    assert all(line.startswith(".commercial-dashboard-v2") for line in selectors)
