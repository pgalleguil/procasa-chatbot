from pathlib import Path

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "templates" / "captacion_list.html"


def _template_source() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def test_captacion_template_compiles():
    Environment(loader=FileSystemLoader(ROOT / "templates")).get_template("captacion_list.html")


def test_sidebar_uses_responsive_glass_surface_in_both_themes():
    template = _template_source()
    assert "height: 100dvh" in template
    assert "rgba(8, 20, 38, 0.82)" in template
    assert "rgba(244, 247, 252, 0.84)" in template
    assert "backdrop-filter: blur(14px) saturate(120%)" in template
    assert 'class="sidebar-footer"' in template
    assert "env(safe-area-inset-bottom" in template


def test_kpi_cards_are_accessible_single_selection_controls():
    template = _template_source()
    assert template.count('type="button" class="kpi-card') == 4
    assert template.count('aria-pressed="{{') == 4
    assert "'is-active' if not current_estado" in template


def test_mobile_rows_follow_the_leads_label_value_pattern():
    template = _template_source()
    assert "grid-template-columns: minmax(104px, 34%) minmax(0, 1fr)" in template
    assert template.count('class="mobile-cell-value"') >= 7
    assert "text-align: left !important" in template


def test_progress_panel_uses_the_centralized_management_goal():
    template = _template_source()
    assert 'class="captacion-progress-panel"' in template
    assert "Meta de gestión de captación" in template
    assert "captacion_goal.today_count" in template
    assert "captacion_goal.week_count" in template
    assert "captacion_goal.days_met" in template
    assert "captacion_goal.expected_to_date" in template
    assert "captacion_goal.executives_met_today" in template
    assert "captacion_goal.days_person_met" in template
    assert "captacion_goal.daily" in template
    assert "captacion_goal.executives" in template
