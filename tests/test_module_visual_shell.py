from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "static" / "css" / "crm_module_shell.css"
TEMPLATES = ("contract_dashboard.html", "visita_dashboard.html", "crm_lead_detail.html", "captacion_detail.html")


def test_all_audited_modules_load_the_shared_shell_and_footer():
    for template_name in TEMPLATES:
        source = (ROOT / "templates" / template_name).read_text(encoding="utf-8")
        assert "/static/css/crm_module_shell.css" in source
        assert 'class="sidebar-footer"' in source
        assert 'aria-label="Abrir menú"' in source


def test_shell_has_glass_fallback_safe_area_and_mobile_cards():
    source = SHELL.read_text(encoding="utf-8")
    assert "height: 100dvh" in source
    assert "backdrop-filter: blur(14px) saturate(120%)" in source
    assert "@supports" in source
    assert "env(safe-area-inset-bottom)" in source
    assert "margin-top: auto" in source
    assert "grid-template-columns: minmax(105px, 38%)" in source
    assert "overflow-x: hidden" in source
