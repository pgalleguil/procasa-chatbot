from pathlib import Path
from fastapi.testclient import TestClient

from crm_review import app

ROOT = Path(__file__).parents[1]
TEMPLATE = (ROOT / "templates" / "crm_leads_list.html").read_text(encoding="utf-8")


def html(query="view=list"):
    with TestClient(app) as client:
        return client.get(f"/crm-leads-review?{query}").text


def test_three_equal_kpi_columns_and_compact_height_contract():
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in TEMPLATE
    assert "min-height: 112px" in TEMPLATE
    assert "gap: 18px" in TEMPLATE


def test_kpi_totals_and_operational_secondary_copy():
    page = html()
    assert 'data-target="9"' in page
    assert "4 gestionados · 5 sin atender" in page
    assert "44,4% del total · 2 sin atender" in page
    assert "55,6% del total · 3 sin atender" in page


def test_kpi_sum_and_backend_card_urls():
    page = html()
    assert 'href="/crm-leads-review?view=list&amp;temperatura=HOT"' in page
    assert 'href="/crm-leads-review?view=list&amp;temperatura=COLD"' in page
    assert page.count('class="summary-card') == 3


def test_zero_unassigned_hides_indicator():
    page = html()
    assert "leads sin asignar" not in page


def test_management_bar_has_four_proportional_segments_and_percentages():
    page = html()
    assert page.count("progress-segment segment-") == 4
    for value in ("55,6%", "22,2%", "11,1%"):
        assert value in page
    assert "Gestionados = En gestión + Visitas + Cerrados" in page


def test_management_bar_filter_urls_and_active_clear():
    page = html("view=list&estado=NEW")
    assert 'class="state-metric is-active"' in page
    assert 'href="/crm-leads-review?view=list"' in page
    assert 'class="progress-segment segment-nuevo is-active"' in page
    assert 'estado=GRUPO_GESTION' in page


def test_hot_and_state_filters_combine_in_backend_review():
    page = html("view=list&temperatura=HOT&estado=NEW")
    assert "Cliente Demo 01" in page
    assert "Cliente Demo 03" not in page
    assert 'temperatura=HOT' in page
    assert 'estado=NEW' in page


def test_clear_restores_total_and_no_active_state():
    page = html("view=list")
    assert 'class="summary-card is-active"' in page
    assert 'class="state-metric is-active"' not in page


def test_table_and_quick_response_contract_remain_present():
    page = html()
    for label in ("Asignado", "Prioridad", "Tipo", "Lead", "Gestión", "Respuesta"):
        assert label in page
    assert page.count('data-quick-result=') == 5
