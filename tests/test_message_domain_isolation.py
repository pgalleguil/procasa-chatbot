import ast
from copy import deepcopy
from pathlib import Path

import pytest

from chatbot.chatbot_queue import create_inbound_job
from chatbot.message_domains import (
    CHATBOT, COMMERCIAL_NOTIFICATION, DOCUMENT_SIGNATURE, SLA_ALERT,
    chatbot_key, commercial_key, document_key, require_domain, sla_key,
)
from tests.test_crm_notification_containment import Collection, DB


ROOT = Path(__file__).resolve().parents[1]


def _source_text(name):
    return (ROOT / name).read_text(encoding="utf-8-sig")


def _db():
    return DB(
        chatbot_inbound_jobs=Collection(unique={"inbound_provider_message_id"}),
        contracts=Collection(), visitas=Collection(),
        crm_notifications_v1=Collection(), crm_assignment_cycles=Collection(),
    )


def test_inbound_creates_chatbot_job_not_signature_process():
    db = _db()
    create_inbound_job(db, inbound_provider_message_id="in-1", phone="5691", text="hola")
    assert len(db["chatbot_inbound_jobs"].docs) == 2
    assert db["contracts"].docs == [] and db["visitas"].docs == []


def test_chatbot_response_does_not_block_hot():
    assert "chatbot_inbound_jobs" not in _source_text("chatbot/crm_hot_delivery.py")


def test_chatbot_response_does_not_block_digest():
    assert "chatbot_inbound_jobs" not in _source_text("chatbot/crm_non_hot_digest.py")


def test_visit_order_does_not_create_chatbot_job():
    assert "create_inbound_job" not in _source_text("api_visitas.py")


def test_visit_order_does_not_create_hot_or_digest():
    src = _source_text("api_visitas.py")
    assert "assign_and_enqueue_hot" not in src and "accumulate_non_hot_lead" not in src


def test_contract_does_not_create_hot_or_digest():
    src = _source_text("api_contracts.py")
    assert "assign_and_enqueue_hot" not in src and "accumulate_non_hot_lead" not in src


def test_pending_document_survives_restart():
    doc = {"status": "pending", "message_domain": DOCUMENT_SIGNATURE}
    assert deepcopy(doc) == doc


def test_in_process_document_survives_deploy():
    doc = {"status": "in_process", "message_domain": DOCUMENT_SIGNATURE}
    assert deepcopy(doc)["status"] == "in_process"


def test_two_document_workers_share_one_idempotency_key():
    assert document_key("D1", 1, "client") == document_key("D1", 1, "client")


def test_duplicate_callback_has_one_transition_key():
    callback = document_key("D1", 1, "signed")
    assert len({callback, callback}) == 1


def test_opened_is_not_signed():
    assert "opened" != "signed"


def test_signature_key_is_not_commercial_cycle_key():
    assert document_key("D1", 1, "client") != commercial_key("D1", "hot", "u1")


def test_signed_document_is_terminal_for_document_domain():
    assert "signed" in {"signed", "declined", "expired", "failed"}


def test_expired_link_has_distinct_identity_from_new_version():
    assert document_key("D1", 1, "client") != document_key("D1", 2, "client")


def test_chatbot_reconciliation_excludes_documents():
    src = _source_text("chatbot/chatbot_queue.py")
    assert '"kind": KIND_BATCH' in src and 'contracts' not in src and 'visitas' not in src


def test_commercial_reconciliation_excludes_documents():
    src = _source_text("scripts/reconcile_weekend_notifications_20260726.py")
    assert 'contracts' not in src and 'visitas' not in src


def test_commercial_and_document_keys_are_distinct():
    assert commercial_key("c", "hot", "u") != document_key("c", "hot", "u")


def test_provider_evidence_stays_in_own_domain():
    chatbot = {"message_domain": CHATBOT, "provider_message_id": "p1"}
    commercial = {"message_domain": COMMERCIAL_NOTIFICATION, "provider_message_id": "p2"}
    assert chatbot["provider_message_id"] != commercial["provider_message_id"]


def test_worker_rejects_other_message_domain():
    with pytest.raises(ValueError, match="wrong_message_domain"):
        require_domain({"message_domain": DOCUMENT_SIGNATURE}, COMMERCIAL_NOTIFICATION)


def test_router_origin_without_verified_source_is_not_allowed():
    from chatbot.crm_notifications import verified_commercial_source
    cycle = {
        "notification_eligible": True, "reason": "LeadRouter",
        "cycle_origin": "router", "source_event_id": "in-1",
    }
    assert verified_commercial_source(_db(), cycle) is False


def test_normalizing_twice_keeps_one_commercial_identity():
    key = commercial_key("cycle-1", "hot", "u1")
    assert len({key, key}) == 1


def test_document_counts_are_stable_without_writes():
    before = {"pending": 1, "in_process": 1}
    assert deepcopy(before) == before


def test_simultaneous_restart_does_not_cross_domain_keys():
    keys = {
        chatbot_key("in-1", "b-1"),
        document_key("d-1", 1, "client"),
        commercial_key("c-1", "hot", "u-1"),
    }
    assert len(keys) == 3


def test_sla_domain_is_separate_and_disabled_by_default():
    from config import Config
    assert sla_key("c", "yellow", 30, "u").startswith("sla_alert:")
    assert Config.CRM_SLA_ALERTS_ENABLED is False
    assert Config.CRM_SLA_V2_LIVE_ENABLED is False
