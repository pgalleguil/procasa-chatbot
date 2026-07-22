import unittest
from pathlib import Path

from analytics.market_indicators import _IndicatorParser


ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "templates" / "analytics" / "commercial_dashboard.html"


class CommercialDashboardPremiumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = TEMPLATE.read_text(encoding="utf-8")

    def test_information_architecture_is_preserved(self):
        for label in (
            "Resumen Ejecutivo",
            "Gesti&oacute;n Comercial",
            "Demanda Inmobiliaria",
            "Fuentes y Propiedades",
            "Calidad de Datos",
        ):
            self.assertIn(label, self.html)

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

    def test_executive_mode_and_separate_quality_panel_exist(self):
        for contract in ('id="teamMode"', 'id="executiveView"', 'id="tab-quality"'):
            self.assertIn(contract, self.html)

    def test_market_parser_reads_only_named_official_rows(self):
        parser = _IndicatorParser()
        parser.feed("""
            <h3>Indicadores diarios (22-jul-2026)</h3>
            <table><tr><td><p>Unidad de Fomento (UF)</p></td><td><p>40.844,79</p></td><td>Pesos</td></tr></table>
        """)
        self.assertEqual(parser.rows[0][0], "Unidad de Fomento (UF)")
        self.assertEqual(parser.rows[0][1], "40.844,79")
        self.assertIn("22-jul-2026", "".join(parser.heading))


if __name__ == "__main__":
    unittest.main()
