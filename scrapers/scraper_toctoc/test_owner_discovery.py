"""Tests for the read-only owner discovery cursor/cache layer."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from owner_discovery import (  # noqa: E402
    DiscoveryLimits,
    OwnerDiscoveryCrawler,
    OwnerDiscoverySegment,
    OwnerDiscoveryStore,
    classify_id_type,
    load_segments,
)


class FakeSource:
    def __init__(self, pages, details):
        self.pages = pages
        self.details = details
        self.detail_calls = []
        self.list_calls = []

    def list_cards(self, segment, page):
        self.list_calls.append((segment.key, page))
        return list(self.pages.get((segment.key, page), []))

    def get_detail(self, url):
        self.detail_calls.append(url)
        return dict(self.details[url])

    def close(self):
        return None


def segment():
    return OwnerDiscoverySegment("metropolitana", "maipu", "casa")


def card(listing_id):
    return {"listing_id": listing_id, "url": f"https://www.toctoc.com/propiedad/casa-maipu-{listing_id}"}


class OwnerDiscoveryTests(unittest.TestCase):
    def make_crawler(self, source, path, **kwargs):
        return OwnerDiscoveryCrawler(
            source,
            OwnerDiscoveryStore(path),
            limits=DiscoveryLimits(**kwargs),
            run_id_factory=lambda: "test-run",
        )

    def test_idtype_mapping_and_fail_closed_unknown(self):
        self.assertEqual(classify_id_type("1"), "OWNER_CANDIDATE")
        self.assertEqual(classify_id_type("2"), "BROKER")
        self.assertEqual(classify_id_type("3"), "OUT_OF_SCOPE_NEW_DEVELOPMENT")
        self.assertEqual(classify_id_type(""), "UNKNOWN")
        self.assertEqual(classify_id_type("99"), "UNKNOWN")

    def test_cursor_resume_does_not_restart_page_one(self):
        seg = segment()
        pages = {(seg.key, 1): [card("1")], (seg.key, 2): [card("2")], (seg.key, 3): []}
        details = {card(str(i))["url"]: {"seller_id_type_raw": "1"} for i in (1, 2)}
        with tempfile.TemporaryDirectory() as tmp:
            source = FakeSource(pages, details)
            first = self.make_crawler(source, Path(tmp) / "state.json", max_pages_per_segment=1)
            first.run([seg])
            second = self.make_crawler(source, Path(tmp) / "state.json", max_pages_per_segment=2)
            second.run([seg])
            self.assertEqual([page for _, page in source.list_calls], [1, 2, 3])

    def test_idtype_cache_skips_known_broker_and_new_development(self):
        seg = segment()
        pages = {(seg.key, 1): [card("1"), card("2"), card("3")], (seg.key, 2): []}
        details = {
            card("1")["url"]: {"seller_id_type_raw": "1"},
            card("2")["url"]: {"seller_id_type_raw": "2"},
            card("3")["url"]: {"seller_id_type_raw": "3"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            source = FakeSource(pages, details)
            self.make_crawler(source, state, max_pages_per_segment=3).run([seg])
            source.detail_calls.clear()
            second = self.make_crawler(source, state, max_pages_per_segment=3)
            result = second.run([seg], reset=True)
            self.assertEqual(source.detail_calls, [])
            self.assertEqual(result.details_skipped_known, 3)
            self.assertEqual(result.owners_found, 1)

    def test_end_of_results_and_owner_yield(self):
        seg = segment()
        pages = {(seg.key, 1): [card("1"), card("2")], (seg.key, 2): []}
        details = {card("1")["url"]: {"seller_id_type_raw": "1"}, card("2")["url"]: {"seller_id_type_raw": "2"}}
        with tempfile.TemporaryDirectory() as tmp:
            source = FakeSource(pages, details)
            result = self.make_crawler(source, Path(tmp) / "state.json", max_pages_per_segment=5).run([seg])
            self.assertEqual(result.stop_reasons, {"END_OF_RESULTS": 1})
            self.assertEqual(result.owners_found, 1)
            self.assertEqual(result.brokers_found, 1)
            self.assertEqual(result.detail_requests_per_owner, 2.0)

    def test_max_pages_stops_without_scanning_beyond_configured_depth(self):
        seg = segment()
        pages = {
            (seg.key, 1): [card("1")],
            (seg.key, 2): [card("2")],
            (seg.key, 3): [card("3")],
        }
        details = {
            card("1")["url"]: {"seller_id_type_raw": "1"},
            card("2")["url"]: {"seller_id_type_raw": "1"},
            card("3")["url"]: {"seller_id_type_raw": "1"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            source = FakeSource(pages, details)
            result = self.make_crawler(
                source,
                Path(tmp) / "state.json",
                max_pages_per_segment=2,
            ).run([seg])
            self.assertEqual([page for _, page in source.list_calls], [1, 2])
            self.assertEqual(result.stop_reasons, {"MAX_PAGES_REACHED": 1})
            self.assertEqual(result.owners_found, 2)

    def test_max_detail_budget_stops_before_an_extra_request(self):
        seg = segment()
        pages = {(seg.key, 1): [card("1"), card("2"), card("3")]}
        details = {card(str(i))["url"]: {"seller_id_type_raw": "1"} for i in (1, 2, 3)}
        with tempfile.TemporaryDirectory() as tmp:
            source = FakeSource(pages, details)
            result = self.make_crawler(source, Path(tmp) / "state.json", max_detail_requests=2).run([seg])
            self.assertEqual(len(source.detail_calls), 2)
            self.assertEqual(result.stop_reasons, {"REQUEST_BUDGET_EXCEEDED": 1})

    def test_per_segment_budget_prevents_one_segment_from_consuming_national_sample(self):
        seg = segment()
        pages = {(seg.key, 1): [card("1"), card("2"), card("3")]}
        details = {card(str(i))["url"]: {"seller_id_type_raw": "1"} for i in (1, 2, 3)}
        with tempfile.TemporaryDirectory() as tmp:
            source = FakeSource(pages, details)
            result = self.make_crawler(
                source,
                Path(tmp) / "state.json",
                max_detail_requests=10,
                max_detail_requests_per_segment=2,
            ).run([seg])
            self.assertEqual(len(source.detail_calls), 2)
            self.assertEqual(result.stop_reasons, {"SEGMENT_DETAIL_BUDGET_EXCEEDED": 1})

    def test_segments_are_national_configuration_not_hardcoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "segments.json"
            path.write_text(json.dumps({"segments": [
                {"region": "biobio", "commune": "concepcion", "property_type": "departamento"},
                {"region": "coquimbo", "commune": "la-serena", "property_type": "casa", "operation": "venta"},
            ]}), encoding="utf-8")
            segments = load_segments(path)
            self.assertEqual([s.region for s in segments], ["biobio", "coquimbo"])
            self.assertEqual(segments[1].property_type, "casa")

    def test_repeated_run_is_idempotent(self):
        seg = segment()
        pages = {(seg.key, 1): [card("1")], (seg.key, 2): []}
        details = {card("1")["url"]: {"seller_id_type_raw": "1"}}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            source = FakeSource(pages, details)
            self.make_crawler(source, state, max_pages_per_segment=5).run([seg])
            calls_after_first = len(source.detail_calls)
            second = self.make_crawler(source, state, max_pages_per_segment=5)
            result = second.run([seg])
            self.assertEqual(len(source.detail_calls), calls_after_first)
            self.assertEqual(result.details_requested, 0)
            self.assertEqual(result.details_skipped_known, 0)
            self.assertEqual(result.owners_found, 0)

    def test_yield_priority_reorders_without_excluding_segments(self):
        first = segment()
        second = OwnerDiscoverySegment("biobio", "concepcion", "casa")
        with tempfile.TemporaryDirectory() as tmp:
            store = OwnerDiscoveryStore(Path(tmp) / "state.json")
            store.save_yield(first, "1-5", details=10, owners=1, pages=1)
            store.save_yield(second, "1-5", details=10, owners=8, pages=1)
            source = FakeSource({(first.key, 1): [], (second.key, 1): []}, {})
            crawler = self.make_crawler(source, Path(tmp) / "state.json")
            ordered = crawler.prioritize_segments([first, second])
            self.assertEqual(ordered, [second, first])
            self.assertEqual({item.key for item in ordered}, {first.key, second.key})


if __name__ == "__main__":
    unittest.main(verbosity=2)
