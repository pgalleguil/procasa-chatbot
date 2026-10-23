"""P0 reconciliation readback optimization: preserve the exact durable alert."""
from datetime import datetime, timezone

import mongomock

from chatbot.crm_assignment_integrity import (
    record_assignment_integrity_alert,
    reconcile_assignment_integrity,
)


class CountingCollection:
    def __init__(self, real):
        self.real = real
        self.readbacks = 0
        self.writes = 0

    def find_one(self, *args, **kwargs):
        self.readbacks += 1
        return self.real.find_one(*args, **kwargs)

    def update_one(self, *args, **kwargs):
        self.writes += 1
        return self.real.update_one(*args, **kwargs)


class CountingDatabase:
    def __init__(self):
        self.real = mongomock.MongoClient().test
        self.alerts = CountingCollection(self.real["crm_assignment_integrity_alerts"])

    def __getitem__(self, key):
        return self.alerts if key == "crm_assignment_integrity_alerts" else self.real[key]


def test_read_back_is_opt_in_for_reconciler_only_and_durable_record_is_unchanged():
    db = CountingDatabase()
    at = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
    sample = dict(lead_id="lead-1", issue_code="canonical_cycle_mirror_inconsistency",
                  evidence={"signature": "a:b"}, detected_at=at, detection_run_id="run-1")

    first = record_assignment_integrity_alert(db, **sample, read_back=False)
    assert first["_id"].startswith("assignment-integrity:")
    assert db.alerts.readbacks == 0
    assert db.alerts.writes == 3
    row = db.alerts.real.find_one({"_id": first["_id"]})
    assert row["observations"] == 1
    assert row["status"] == "open"
    assert row["review_required"] is True
    assert row["evidence"] == sample["evidence"]

    record_assignment_integrity_alert(db, **sample, read_back=False)
    assert db.alerts.readbacks == 0
    assert db.alerts.writes == 6
    assert db.alerts.real.find_one({"_id": first["_id"]})["observations"] == 1

    record_assignment_integrity_alert(db, **{**sample, "detection_run_id": "run-2"},
                                      read_back=False)
    assert db.alerts.real.find_one({"_id": first["_id"]})["observations"] == 2
    assert db.alerts.readbacks == 0

    # Other callers retain the existing materialized return value by default.
    actual = record_assignment_integrity_alert(db, **sample)
    assert db.alerts.readbacks == 1
    assert actual["observations"] == 3


def test_reconciler_emits_same_findings_and_writes_without_readbacks():
    db = CountingDatabase()
    db.real["leads"].insert_one({
        "_id": "lead-no-cycle", "ejecutivo_asignado": "Hernán Castro",
        "prospecto": {"ejecutivo": "Hernán Castro", "codigo": "6732"},
        "lifecycle": {},
    })
    report = reconcile_assignment_integrity(
        db, now=datetime(2026, 10, 23, 14, tzinfo=timezone.utc), persist_alerts=True,
    )
    assert report["leads_checked"] == 1
    assert report["findings"] >= 1
    assert report["alerts_upserted"] == report["findings"]
    assert db.alerts.readbacks == 0
    assert db.alerts.writes == 3 * report["findings"]
    assert db.alerts.real.count_documents({}) >= 1
