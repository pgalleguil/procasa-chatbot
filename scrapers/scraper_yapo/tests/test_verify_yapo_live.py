from types import SimpleNamespace

from scrapers.scraper_yapo.staging import OPERATIONAL_COLLECTION, SOURCE_PORTAL
from scrapers.scraper_yapo.verify_yapo_live import _record_check


class _FakeCollection:
    def __init__(self):
        self.query = None
        self.update = None

    def update_one(self, query, update):
        self.query = query
        self.update = update
        return SimpleNamespace(matched_count=1)


def test_operational_availability_write_preserves_crm_and_staging_fields():
    collection = _FakeCollection()
    item = {
        "availability_status": "ACTIVO",
        "availability_checked_at": "2026-11-04T17:00:00+00:00",
        "availability_evidence": ["active_marker_explicit"],
        "http_status": 200,
        "html_validation_status": "OK",
        "html_sha256": "abc123",
        "local_html_path": "local.html",
        "run_id": "run-1",
    }

    _record_check(collection, "33190839", item, target=OPERATIONAL_COLLECTION)

    assert collection.query == {"origen": SOURCE_PORTAL, "listing_id": "33190839"}
    assert collection.update["$set"]["availability_status"] == "ACTIVO"
    assert collection.update["$set"]["availability_source"] == "flashproxy"
    assert not ({"staging_only", "crm_promotion_allowed", "distribution_blocked", "assignment_ready"} & set(collection.update["$set"]))
    assert set(collection.update) == {"$set", "$push"}
    assert collection.update["$push"]["availability_checks"]["source"] == "flashproxy"
