import pytest

from scrapers.scraper_yapo.import_yapo_live_batch import _finalize_record, _staging_drop_snapshot
from scrapers.scraper_yapo.staging import SOURCE_PORTAL


def test_removed_live_listing_stays_uncertain_and_unassignable():
    record = _finalize_record({
        "listing_id": "33215722",
        "html_validation_status": "LISTING_REMOVED",
        "availability_status": "DESACTIVADO",
        "availability_source": "flashproxy",
        "extraction_issues": ["html_listing_removed"],
        "classification": {},
    })

    assert record["classification"]["state"] == "INCIERTO"
    assert record["availability_status"] == "DESACTIVADO"
    assert record["scrape_stage"] == "ad_removed"
    assert record["classification"]["assignment_ready"] is False
    assert record["classification"]["exclude_from_assignment"] is True
    assert record["classification_debug"]["deepseek_call_executed"] is False


def test_detail_error_remains_unverified_and_requires_rescrape():
    record = _finalize_record({
        "listing_id": "33213261",
        "availability_status": "NO_VERIFICADO",
        "availability_source": "flashproxy",
        "extraction_issues": ["detail_error:RuntimeError"],
        "classification": {},
    })

    assert record["classification"]["state"] == "INCIERTO"
    assert record["availability_status"] == "NO_VERIFICADO"
    assert record["scrape_stage"] == "needs_rescrape"
    assert record["classification"]["assignment_ready"] is False
    assert record["assignment_ready"] is False
    assert record["classification_debug"]["deepseek_call_executed"] is False


class _FakeStaging:
    def __init__(self, documents):
        self.documents = documents

    def count_documents(self, query):
        if query:
            return sum(all(document.get(key) == value for key, value in query.items()) for document in self.documents)
        return len(self.documents)

    def find(self, query):
        return [document for document in self.documents if all(document.get(key) == value for key, value in query.items())]


def test_staging_drop_snapshot_rejects_unexpected_non_yapo_documents():
    records = [
        {"source_portal": SOURCE_PORTAL, "listing_id": str(index)}
        for index in range(2)
    ]
    staging = _FakeStaging(records + [{"source_portal": "other", "listing_id": "x"}])

    with pytest.raises(RuntimeError, match="temporary collection must contain exactly 2 unique Yapo records"):
        _staging_drop_snapshot(staging, expected_count=2)


def test_staging_drop_snapshot_accepts_only_exact_unique_yapo_set():
    records = [
        {"source_portal": SOURCE_PORTAL, "listing_id": str(index)}
        for index in range(2)
    ]

    found, ids = _staging_drop_snapshot(_FakeStaging(records), expected_count=2)

    assert len(found) == 2
    assert ids == {"0", "1"}
