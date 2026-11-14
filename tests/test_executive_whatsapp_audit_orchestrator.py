"""Supervised audit orchestration, synthetic leads and fake model only."""
import json
from copy import deepcopy

import pytest

from chatbot.executive_whatsapp_audit_orchestrator import evaluate_lead_in_isolation
from tests.test_executive_whatsapp_audit import (
    FakeCollection, FakeDB, FakeResult, event, ingest,
)


class LeadCollection(FakeCollection):
    def find_one(self, selector, projection=None):
        if list(selector) == ["_id"]:
            for row in self.db.leads:
                if row["_id"] == selector["_id"]:
                    return deepcopy(row)
            return None
        for row in self.db.leads:
            if row["_id"] != selector["_id"]:
                continue
            wanted = selector.get("executive_whatsapp_messages.provider_key")
            if wanted and any(x.get("provider_key") == wanted for x in row.get("executive_whatsapp_messages", [])):
                return deepcopy(row)
        return None

    def update_one(self, selector, update, upsert=False):
        if "$set" in update:
            assert set(update["$set"]) == {"executive_whatsapp_audit"}
            for row in self.db.leads:
                if row["_id"] == selector["_id"]:
                    row["executive_whatsapp_audit"] = deepcopy(
                        update["$set"]["executive_whatsapp_audit"]
                    )
                    return FakeResult(1)
            return FakeResult(0)
        return super().update_one(selector, update, upsert)


class DB(FakeDB):
    def __getitem__(self, key):
        if key == "leads":
            return LeadCollection(key, self)
        return super().__getitem__(key)


def filled_db():
    db = DB()
    ingest(db, event(ident="out", outgoing=True), commit=True)
    ingest(db, event(ident="in", outgoing=False), commit=True)
    return db


def model_for(db, calls):
    keys = [x["provider_key"] for x in db.leads[0]["executive_whatsapp_messages"]]
    def model(request):
        calls.append(request)
        return json.dumps({
            "commercial_status": "ACTIVE_INTEREST",
            "contact_effective": True,
            "follow_up_required": True,
            "opt_out": False,
            "next_action": "Revisar seguimiento",
            "preferences": {},
            "evidence_provider_keys": keys,
            "uncertainty": "Historial anterior no recuperado",
            "coverage_complete": False,
        })
    return model


def test_dryrun_calls_model_once_and_never_writes_audit():
    db = filled_db()
    original = deepcopy(db.leads[0])
    calls = []
    result = evaluate_lead_in_isolation(
        db, lead_id="L1", administrator_user_id="admin-1",
        model_call=model_for(db, calls),
    )
    assert result["status"] == "audit_evaluated"
    assert result["model_calls"] == 1
    assert result["writes"] == 0
    assert result["persistence"] == "planned"
    assert db.leads[0] == original
    assert calls[0]["no_automatic_actions"] is True


def test_opt_in_writes_only_pending_audit_without_sla_or_chatbot_changes():
    db = filled_db()
    original = deepcopy(db.leads[0])
    result = evaluate_lead_in_isolation(
        db, lead_id="L1", administrator_user_id="admin-1",
        model_call=model_for(db, []), persist_candidate=True,
    )
    assert result["writes"] == 1
    assert result["campaign_eligible"] is False
    assert db.leads[0]["messages"] == original["messages"]
    assert db.leads[0]["executive_whatsapp_messages"] == original["executive_whatsapp_messages"]
    assert db.leads[0]["executive_whatsapp_audit"]["review_status"] == "PENDING_HUMAN_REVIEW"


def test_no_executive_evidence_never_calls_model():
    db = DB()
    calls = []
    result = evaluate_lead_in_isolation(
        db, lead_id="L1", administrator_user_id="admin-1",
        model_call=lambda arg: calls.append(arg),
    )
    assert result["status"] == "no_executive_chat_evidence"
    assert calls == []


def test_untrusted_model_output_is_rejected_without_audit_write():
    db = filled_db()
    with pytest.raises(ValueError):
        evaluate_lead_in_isolation(
            db, lead_id="L1", administrator_user_id="admin-1",
            model_call=lambda _: '{"commercial_status":"FAKE"}',
            persist_candidate=True,
        )
    assert "executive_whatsapp_audit" not in db.leads[0]


def test_non_administrator_context_is_blocked():
    db = filled_db()
    with pytest.raises(PermissionError):
        evaluate_lead_in_isolation(
            db, lead_id="L1", administrator_user_id="",
            model_call=model_for(db, []),
        )
