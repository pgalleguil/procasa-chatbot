import unittest
import re
from pathlib import Path
from analytics.leads_queries import _build_extra_filter, _commune_distribution


ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "templates" / "analytics" / "commercial_dashboard.html"


class CommercialDashboardPremiumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = TEMPLATE.read_text(encoding="utf-8")

    def test_information_architecture_is_preserved(self):
        for label in (
            "Resumen Ejecutivo",
            "Equipo y Cobertura",
            "Demanda Inmobiliaria",
            "Fuentes y Propiedades",
            "Calidad de Datos",
        ):
            self.assertIn(label, self.html)

    def test_macro_ticker_is_fully_removed(self):
        for obsolete in ("marketStrip", "marketTrack", "loadMarketIndicators", "market-indicators"):
            self.assertNotIn(obsolete, self.html)

    def test_required_loading_and_accessibility_states_exist(self):
        for contract in (
            'id="cdProgress"',
            'aria-busy="true"',
            'aria-live="polite"',
            'id="btnRetry"',
            "AbortController",
            "RESPONSE_CACHE",
            "prefers-reduced-motion",
        ):
            self.assertIn(contract, self.html)

    def test_zero_denominator_and_hidden_dates_are_explicit(self):
        self.assertIn("Sin base porcentual comparable", self.html)
        self.assertIn('id="periodCustom" hidden inert aria-hidden="true"', self.html)
        self.assertNotIn("Infinity", self.html)

    def test_executive_mode_and_separate_quality_panel_exist(self):
        for contract in ('id="teamMode"', 'id="executiveView"', 'id="tab-quality"'):
            self.assertIn(contract, self.html)

    def test_commune_distribution_keeps_missing_values_as_si(self):
        rows = _commune_distribution([
            {"prospecto": {"comuna": "Providencia"}},
            {"prospecto": {"comuna": "Providencia"}},
            {"prospecto": {}},
        ])
        self.assertEqual(rows[0], {"value": "Providencia", "count": 2})
        self.assertIn({"value": "S/I", "count": 1}, rows)

    def test_stage_filter_uses_canonical_pipeline_stage(self):
        self.assertEqual(_build_extra_filter({"stage": "CONTACTED"}), {"pipeline_stage": "CONTACTED"})
        self.assertIn('id="fStage"', self.html)

    def test_visual_system_v2_contract(self):
        for token in (
            "--font-ui:", "--bg-canvas:#f6f7f9", "--bg-surface:#fff",
            "--text-primary:#101828", "--brand-primary:#203b8f",
            "--radius-card:14px", "--shadow-card:", "--motion-fast:140ms",
            '[data-theme="dark"]', "--bg-canvas:#0b0d12",
        ):
            self.assertIn(token, self.html)
        for title in ("Leads recibidos", "Hot actuales", "Intención de visita", "Cumplimiento SLA"):
            self.assertIn(title, self.html)
        for rejected in ("LEADS RECIBIDOS", "INT. DE VISITA", "VISITAS COORD.", "ErrorReintentar", "Filtros 0"):
            self.assertNotIn(rejected, self.html)

    def test_ids_are_unique_and_sidebar_theme_is_accessible(self):
        ids = re.findall(r'\bid="([^"]+)"', self.html)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn('id="btnTheme" aria-label="Cambiar tema"', self.html)
        self.assertNotIn('id="btnThemeHeader"', self.html)
        self.assertIn("localStorage.getItem('theme')", self.html.split("</head>", 1)[0])

    def test_temporal_context_uses_backend_metadata(self):
        for contract in (
            'id="temporalContext" aria-busy="true"',
            'id="cmpMode"',
            "period.comparison||period.previous",
            "getPeriodContext(",
            "renderPeriodContext(",
            "PERIOD_TYPE_LABELS",
        ):
            self.assertIn(contract, self.html)
        self.assertNotIn('id="perCur">&mdash;', self.html)
        self.assertNotIn('hidden>0</span>', self.html)

    def test_advanced_filters_have_one_accessible_name(self):
        for control_id in ("fExecPrimary", "fExec", "fSource", "fOp", "fType", "fCommune", "fTemp", "fProp", "fAsgn", "fStage"):
            self.assertNotIn(f'<label for="{control_id}" class="cd-fhide">', self.html)
            self.assertRegex(self.html, rf'id="{control_id}"[^>]*aria-label="[^"]+"')


if __name__ == "__main__":
    unittest.main()
