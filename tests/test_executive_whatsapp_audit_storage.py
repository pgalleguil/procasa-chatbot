"""Audit snapshot persistence tests: self-contained synthetic Mongo stub only."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from chatbot.executive_whatsapp_audit_storage import persist_audit_candidate

EVIDENCE = "wasender:synthetic-hash"
WHEN = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)


class Result:
    def __init__(self, changed):
        self.modified_count = changed


class Leads:
    def __init__(self):
        self.doc = {
            "_id": "L1",
            "messages": [{"role": "assistant", "content": "chatbot preserved"}],
            "lifecycle": {"lead_temperature_effective": "HOT"},
            "sla_first_management_status": "PENDING",
            "executive_whatsapp_messages": [{"provider_key": EVIDENCE}],
        }
        self.last_filter = None
        self.last_update = None
        self.writes = 0

    def find_one(self, query, projection=None):
        if query.get("_id") != "L1":
            return None
        return deepcopy(self.doc)

    def update_one(self, selector, update, upsert=False):
        assert upsert is False
        self.last_filter = selector
        self.last_update = update
        assert set(update) == {"$set"}
        assert list(update["$set"]) == ["executive_whatsapp_audit"]
        keys = selector["executive_whatsapp_messages.provider_key"]["$all"]
        if not all(key in [x["provider_key"] for x in self.doc["executive_whatsapp_messages"]] for key in keys):
            return Result(0)
        self.doc["executive_whatsapp_audit"] = deepcopy(
            update["$set"]["executive_whatsapp_audit"]
        )
        self.writes += 1
        return Result(1)


class DB:
    def __init__(self):
        self.leads = Leads()

    def __getitem__(self, name):
        assert name == "leads"
        return self.leads


def candidate(**kwargs):
    item = {
        "review_status": "PENDING_HUMAN_REVIEW",
        "commercial_status": "ACTIVE_INTEREST",
        "contact_effective": True,
        "follow_up_required": True,
        "opt_out": False,
        "coverage_complete": False,
        "evidence_provider_keys": [EVIDENCE],
        "uncertainty": "Partial history",
        "next_action": "Personal info +56912345678",
        "preferences": {"secret": "client@example.com"},
    }
    item.update(kwargs)
    return item


def run(db, value=None, commit=False, admin="admin-1"):
    return persist_audit_candidate(
        db, lead_id="L1", admin_user_id=admin,
        candidate=candidate() if value is None else value,
        commit=commit, created_at=WHEN,
    )


def test_dryrun_keeps_lead_intact_and_never_contacts_clients():
    db = DB()
    original = deepcopy(db.leads.doc)
    assert run(db) == {"status": "planned", "written": False}
    assert db.leads.doc == original
    assert db.leads.writes == 0


def test_explicit_commit_writes_only_audit_pending_review():
    db = DB()
    original = deepcopy(db.leads.doc)
    assert run(db, commit=True) == {"status": "pending_review_saved", "written": True}
    written = db.leads.doc
    assert written["messages"] == original["messages"]
    assert written["lifecycle"] == original["lifecycle"]
    assert written["sla_first_management_status"] == original["sla_first_management_status"]
    assert written["executive_whatsapp_messages"] == original["executive_whatsapp_messages"]
    audit = written["executive_whatsapp_audit"]
    assert audit["review_status"] == "PENDING_HUMAN_REVIEW"
    assert audit["campaign_eligible"] is False
    assert audit["changes_crm_management"] is False
    assert "client@example.com" not in str(audit)
    assert "+56912345678" not in str(audit)
    assert "$expr" in db.leads.last_filter
    assert db.leads.writes == 1


def test_idempotent_same_revision_and_no_new_write():
    db = DB()
    run(db, commit=True)
    assert run(db, commit=True) == {"status": "duplicate_confirmed", "written": False}
    assert db.leads.writes == 1


def test_missing_or_unverified_evidence_refuses_write():
    db = DB()
    value = candidate(evidence_provider_keys=["fake-source"])
    assert run(db, value=value, commit=True) == {
        "status": "evidence_not_in_executive_messages", "written": False
    }
    assert db.leads.writes == 0


def test_missing_privileged_context_does_not_write():
    db = DB()
    with pytest.raises(ValueError, match="authenticated_admin_required"):
        run(db, admin="")
    assert db.leads.writes == 0


@pytest.mark.parametrize("bad", [
    candidate(review_status="APPROVED_HUMAN_REVIEW"),
    candidate(commercial_status="FABRICATED"),
    candidate(evidence_provider_keys=[]),
    candidate(uncertainty="x" * 700),
    candidate(contact_effective=1),
])
def test_untrusted_audit_candidates_rejected(bad):
    db = DB()
    with pytest.raises(ValueError):
        run(db, value=bad, commit=True)
    assert db.leads.writes == 0


def test_missing_lead_refuses_creating_one():
    db = DB()
    result = persist_audit_candidate(
        db, lead_id="nonexistent", admin_user_id="admin",
        candidate=candidate(), commit=True, created_at=WHEN
    )
    assert result["status"] == "lead_not_found"
    assert db.leads.writes == 0
