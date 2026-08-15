"""Contract and live regression tests for Toctoc gw-lista-seo discovery.

The live suite is intentionally opt-in because it traverses the current public
inventory and never writes MongoDB or downloads detail HTML.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scraper_toctoc"))

from discovery import build_gw_lista_request, discover_listing_urls  # noqa: E402
from run_toctoc import _build_parser  # noqa: E402


class GwListaContractTests(unittest.TestCase):
    def test_request_contains_current_contract_and_auditable_metadata(self):
        filtros = [{
            "id": "comuna", "name": "Comuna", "type": "select",
            "values": [{"id": 342, "label": "Macul", "value": [342]}],
        }]
        req = build_gw_lista_request(
            base_url="https://www.toctoc.com", filtros=filtros,
            operacion="venta", tipo="departamento", region="metropolitana",
            comuna="macul", page=2,
        )
        self.assertIn("/gw-lista-seo/propiedades?", req["url"])
        self.assertIn("order=1", req["url"])
        self.assertIn("page=2", req["url"])
        self.assertEqual(req["effective_numeric_filters"]["comuna"][0]["id"], 342)
        self.assertEqual(req["requested_filters"]["comuna"], "macul")

    def test_limits_are_explicit_and_default_to_full_inventory(self):
        parser = _build_parser()
        defaults = parser.parse_args(["discover"])
        self.assertIsNone(defaults.max_pages)
        self.assertIsNone(defaults.max_urls)
        limited = parser.parse_args(["discover", "--max-pages", "2", "--max-urls", "7"])
        self.assertEqual(limited.max_pages, 2)
        self.assertEqual(limited.max_urls, 7)


def run_live() -> int:
    cases = [
        ("macul_venta", "venta", "macul", 20),
        ("macul_arriendo", "arriendo", "macul", 1),
        ("penalolen_venta", "venta", "penalolen", 1),
        ("penalolen_arriendo", "arriendo", "penalolen", 2),
    ]
    reports = []
    ok = True
    for name, operacion, comuna, minimum in cases:
        batch_id = f"gw_lista_test_{name}"
        items = discover_listing_urls(
            max_pages=None, max_urls=None, batch_id=batch_id,
            use_playwright=False, operacion=operacion, tipo="departamento",
            region="metropolitana", comuna=comuna,
        )
        report_path = ROOT / "scraper_toctoc" / "reports" / f"discovery_gw_lista_{batch_id}.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        pages = report.get("pages", [])
        total = report.get("pages", [{}])[0].get("total_reported") if pages else 0
        page_size = pages[0].get("page_size", 0) if pages else 0
        expected = math.ceil(total / page_size) if total and page_size else 0
        precision = report.get("commune_precision", 0)
        page_sets = [set(p.get("listing_ids", [])) for p in pages]
        result = {
            "case": name, "total": total, "pages_expected": expected,
            "pages_processed": report.get("pages_processed"),
            "raw_urls": report.get("raw_urls"), "unique_urls": len(items),
            "commune_precision": precision,
            "stop_reason": report.get("stop_reason"),
            "page_size": page_size,
            "minimum_expected": minimum,
        }
        reports.append(result)
        if not items or precision < 0.95 or len(pages) != expected or len(items) < minimum:
            ok = False
        for left, right in zip(pages, pages[1:]):
            if left.get("page_signature") == right.get("page_signature"):
                ok = False
        for index, current in enumerate(page_sets):
            for following in page_sets[index + 1:]:
                if current & following:
                    ok = False
        print(json.dumps(result, ensure_ascii=False))
    print(json.dumps({"passed": ok, "cases": reports}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.live:
        raise SystemExit(run_live())
    unittest.main(argv=[sys.argv[0]])
