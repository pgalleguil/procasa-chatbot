from datetime import datetime, timezone

import pytest

from analytics.control_commitments import (
    get_meeting_commitments,
    save_meeting_commitment,
    update_meeting_commitment,
)


class _Collection:
    def __init__(self):
        self.rows = {}

    def find_one(self, query, projection=None):
        return self.rows.get((query["executive"], query["meeting_month"]))

    def update_one(self, query, update, upsert=False):
        key = (query["executive"], query["meeting_month"])
        if "commitments.id" in query:
            row = self.rows[key]
            item = next((item for item in row["commitments"] if item["id"] == query["commitments.id"]), None)
            if not item:
                return type("Result", (), {"matched_count": 0})()
            item.update({"status": update["$set"]["commitments.$.status"],
                         "evidence": update["$set"]["commitments.$.evidence"]})
            return type("Result", (), {"matched_count": 1})()
        row = self.rows.setdefault(key, dict(update["$setOnInsert"]))
        row.update(update["$set"])
        row.setdefault("commitments", []).append(update["$push"]["commitments"])
        return type("Result", (), {"matched_count": 1})()


class _DB:
    def __init__(self):
        self.collection = _Collection()

    def __getitem__(self, name):
        assert name == "crm_control_meeting_commitments"
        return self.collection


def test_commitments_store_meeting_agreements_separately_and_retain_prior_month():
    db = _DB()
    save_meeting_commitment(
        db, executive="Executive D", meeting_month="2026-08", actor="Jefa",
        payload={"action": "Llamar a los leads abiertos", "responsible": "Executive D",
                 "due_date": "2026-08-31", "meeting_date": "2026-08-05",
                 "next_review_date": "2026-09-05"},
        now=datetime(2026, 8, 5, tzinfo=timezone.utc),
    )
    result = save_meeting_commitment(
        db, executive="Executive D", meeting_month="2026-09", actor="Jefa",
        payload={"action": "Actualizar visitas", "responsible": "Executive D",
                 "due_date": "2026-09-30"},
        now=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    panel = get_meeting_commitments(db, executive="Executive D", meeting_month="2026-09")
    assert panel["independent_of_crm"] is True
    assert panel["previous_meeting"]["meeting_month"] == "2026-08"
    assert panel["previous_meeting"]["commitments"][0]["action"] == "Llamar a los leads abiertos"
    assert panel["current_meeting"]["commitments"][0]["id"] == result["commitment"]["id"]


def test_commitment_status_and_evidence_are_explicitly_updated():
    db = _DB()
    saved = save_meeting_commitment(
        db, executive="Ejecutiva", meeting_month="2026-09", actor="Supervisor",
        payload={"action": "Contactar", "responsible": "Ejecutiva", "due_date": "2026-09-30"},
    )
    updated = update_meeting_commitment(
        db, executive="Ejecutiva", meeting_month="2026-09",
        commitment_id=saved["commitment"]["id"], status="completed", evidence="Registro CRM 123",
    )
    assert updated["saved"] is True
    item = get_meeting_commitments(db, executive="Ejecutiva", meeting_month="2026-09")["current_meeting"]["commitments"][0]
    assert item["status"] == "completed"
    assert item["evidence"] == "Registro CRM 123"


def test_invalid_commitment_data_is_rejected_before_writing():
    with pytest.raises(ValueError, match="action_and_responsible_required"):
        save_meeting_commitment(
            _DB(), executive="Ejecutiva", meeting_month="2026-09", actor="Supervisor",
            payload={"action": "", "responsible": "", "due_date": "2026-09-30"},
        )
