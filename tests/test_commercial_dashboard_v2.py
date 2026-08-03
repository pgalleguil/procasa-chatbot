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


def test_final_executive_refinement_contracts_are_present():
    html = TEMPLATE.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    order_fn = html.split("function reorderExecutiveBlocks()", 1)[1].split("function sanitizeUnavailableUi", 1)[0]
    assert order_fn.index("$('executiveStory')") < order_fn.index("commercial-v2-ops-grid") < order_fn.index("cd-grid.cd-g-exec")
    for value in ("kpi-critical", "commercial-sla-critical-pill", "evolutionLegend", "cd-sla-empty", "cd-funnel-collapsed", "btnFunnelExpand", "Sin información suficiente"):
        assert value in html
    for value in ("#fef2f2", "#fecaca", "repeat(3, minmax(0, 1fr))", "height: 260px", "height: 240px", "height: 220px"):
        assert value in css


def test_v2_css_uses_only_scoped_component_selectors():
    css = CSS.read_text(encoding="utf-8")
    selectors = [line.strip() for line in css.splitlines() if "{" in line and not line.lstrip().startswith("@")]
    assert selectors
    assert all(line.startswith(".commercial-dashboard-v2") for line in selectors)


def test_header_comparison_sla_and_initial_scroll_contracts_are_present():
    html = TEMPLATE.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    for value in (
        "PROCASA · INTELIGENCIA COMERCIAL",
        "Control Comercial",
        "Leads, gestión, SLA y avance del período",
        "history.scrollRestoration='manual'",
        "window.scrollTo({top:0,left:0,behavior:'auto'})",
        "event.persisted",
        "vs. '+ctx.previousLabel",
        "Leads abiertos: estado actual",
        "Leads ya gestionados: resultado SLA",
        "No hubo Lead Hot evaluables en el período",
        "slaDefinitionToggle",
        "Sin trazabilidad suficiente",
        "Cobertura SLA no reconciliada",
    ):
        assert value in html
    for value in (
        "position: absolute; left: 0; right: 0; bottom: 0",
        "commercial-v2-type",
        "min-height: 300px",
        "height: 260px",
        "height: 220px",
        "cd-sla-managed-row",
    ):
        assert value in css
