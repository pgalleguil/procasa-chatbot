from pathlib import Path

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "templates" / "captacion_list.html"


def _template_source() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def test_captacion_template_compiles():
    environment = Environment(loader=FileSystemLoader(ROOT / "templates"))
    environment.get_template("captacion_list.html")


def test_sidebar_uses_responsive_glass_surface_in_both_themes():
    template = _template_source()

    assert "height: 100dvh" in template
    assert "rgba(8, 20, 38, 0.82)" in template
    assert "rgba(244, 247, 252, 0.84)" in template
    assert "backdrop-filter: blur(14px) saturate(120%)" in template
    assert "-webkit-backdrop-filter: blur(14px) saturate(120%)" in template
    assert "class=\"sidebar-main-nav\"" in template
    assert "class=\"sidebar-footer\"" in template
    assert "margin-top: auto" in template
    assert "env(safe-area-inset-bottom" in template
    assert ".nav-link i" in template and "opacity: 1" in template


def test_kpi_cards_are_accessible_single_selection_controls():
    template = _template_source()

    assert template.count('type="button" class="kpi-card') == 4
    assert template.count('aria-pressed="{{') == 4
    assert "'is-active' if not current_estado" in template
    assert "current_estado == 'GRUPO_GESTION'" in template
    assert "current_estado == 'GRUPO_CAPTADO'" in template
    assert "current_estado == 'GRUPO_DESCARTADO'" in template


def test_only_clean_captacion_script_is_executable():
    template = _template_source()

    assert 'id="legacy-captacion-script-disabled"' in template
    assert 'id="legacy-captacion-script-duplicate-disabled"' in template
    assert template.count('<script type="text/plain"') == 2
    assert "function updateThemeControl(theme)" in template
    assert "function closeMobileMenu()" in template
    assert "function filterByGrupoEstado(grupo)" in template
    assert "params.set('page', '1')" in template


def test_mobile_menu_control_is_accessible():
    template = _template_source()

    assert 'class="hamburger-mobile"' in template
    assert 'aria-controls="sidebar"' in template
    assert 'aria-expanded="false"' in template
    assert 'aria-label="Abrir menú"' in template
