"""Read-only aggregation contract tests for CRM lead assignment cohorts."""
from datetime import datetime, timezone, timedelta

import pytest

from chatbot.executive_whatsapp_cohort import (
    assignment_cohort_pipeline, read_assignment_cohort,
)


NOW = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)


def test_30day_window_filters_assignment_not_lead_creation():
    p = assignment_cohort_pipeline(days=30, as_of=NOW)
    assert len(p) == 3
    assert p[0]["$project"]["assigned_at_utc"]["$convert"]["input"] == "$assigned_at"
    m = p[1]["$match"]["assigned_at_utc"]
    assert m["$gte"] == NOW - timedelta(days=30)
    assert m["$lte"] == NOW
    assert "$facet" in p[2]
    assert "by_executive" in p[2]["$facet"]


def test_60day_window_and_utc_normalization():
    chile_offset = timezone(timedelta(hours=-3))
    local = datetime(2026, 11, 11, 12, 0, tzinfo=chile_offset)
    p = assignment_cohort_pipeline(days=60, as_of=local)
    m = p[1]["$match"]["assigned_at_utc"]
    assert m["$gte"] == NOW - timedelta(days=60)
    assert m["$lte"] == NOW


@pytest.mark.parametrize("days", [0, 7, -30, 31, 61, True, "30"])
def test_reject_unsupported_or_unbounded_cohort(days):
    with pytest.raises(ValueError, match="unsupported_cohort_window"):
        assignment_cohort_pipeline(days=days, as_of=NOW)


def test_requires_timezone_aware_cutoff():
    with pytest.raises(ValueError, match="timezone_aware"):
        assignment_cohort_pipeline(as_of=datetime(2026, 11, 11))


def test_only_read_stages_and_no_phone_number_projection():
    pipeline = assignment_cohort_pipeline(as_of=NOW)
    serialized = repr(pipeline)
    for forbidden in ("$out", "$merge", "phone", "messages", "$set", "$lookup"):
        assert forbidden not in serialized
    assert [next(iter(stage)) for stage in pipeline] == [
        "$project", "$match", "$facet",
    ]


class ReadOnlyCollection:
    def __init__(self):
        self.pipeline = None
        self.calls = 0

    def aggregate(self, pipeline):
        self.pipeline = pipeline
        self.calls += 1
        return [{
            "total": [{"unique_leads": 151, "assignment_cycles": 263}],
            "by_executive": [
                {"_id": {"user_id": "u1", "display_name": "Ejecutivo 1"},
                 "unique_leads_for_executive": 12,
                 "assignment_cycles": 18},
            ],
        }]


class ReadOnlyDB:
    def __init__(self):
        self.collection = ReadOnlyCollection()

    def __getitem__(self, key):
        assert key == "crm_assignment_cycles"
        return self.collection


def test_aggregate_report_only_returns_counts_without_pii():
    db = ReadOnlyDB()
    report = read_assignment_cohort(db, days=30, as_of=NOW)
    assert report["unique_leads"] == 151
    assert report["assignment_cycles"] == 263
    assert report["by_executive"] == [{
        "executive_user_id": "u1", "display_name": "Ejecutivo 1",
        "leads": 12, "assignment_cycles": 18,
    }]
    assert report["interpretation"] == "assigned_leads_not_recovered_conversations"
    assert report["read_only"] is True
    assert db.collection.calls == 1
