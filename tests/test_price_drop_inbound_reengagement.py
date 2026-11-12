"""Regression for real customer-initiated inbound after a completed property search.

This is not permission for unsolicited price-drop campaigns or follow-ups.
"""
from copy import deepcopy

from chatbot.price_drop_campaign_classifier import lead_must_not_be_contacted
from chatbot.price_drop_leads_v2 import (
    _contact_suppression_reasons,
    direct_inbound_reengagement_allowed,
)


def completed_search_lead():
    return {
        "messages": [
            {"role": "user", "content": "Hola, necesito un departamento"},
            {"role": "assistant", "content": "Le puedo ayudar"},
            {"role": "user", "content": "Ya compré una propiedad"},
        ],
        "price_drop_v2": {"campaigns": [{
            "campaign_id": "older_qa",
            "status": "RESPONDED",
            "last_customer_text": "Ya compré una propiedad",
            "test_mode": True,
        }], "opted_out": False},
    }


def test_completed_search_blocks_campaign_but_not_new_inbound_greeting():
    lead = completed_search_lead()
    before = deepcopy(lead)
    assert lead_must_not_be_contacted(lead) is True
    assert _contact_suppression_reasons(lead) == {"completed_search"}
    assert direct_inbound_reengagement_allowed(lead, "Hola") is True
    assert direct_inbound_reengagement_allowed(lead, "Hola, ¿siguen atendiendo?") is True
    assert lead_must_not_be_contacted(lead) is True  # still no unsolicited campaigns
    assert lead == before  # historical evidence, campaign permissions unchanged


def test_no_override_of_global_opt_out_and_contact_block():
    for reason in (
        {"price_drop_v2": {"opted_out": True}},
        {"messages": [{"role": "user", "content": "No me escriban más"}]},
        {"conversation_status": "STOPPED_BY_CLIENT",
         "conversation_status_reason": "explicit_stop_request"},
        {"price_drop_v2": {"campaigns": [{"status": "OPTED_OUT"}]}},
    ):
        lead = {**completed_search_lead(), **reason}
        assert lead_must_not_be_contacted(lead) is True
        assert direct_inbound_reengagement_allowed(lead, "Hola") is False


def test_new_inbound_stop_or_completed_search_does_not_reactivate():
    lead = completed_search_lead()
    for text in ("", "  ", "STOP", "No me escriban más",
                 "Ya encontré una casa", "Ya compré una propiedad"):
        assert direct_inbound_reengagement_allowed(lead, text) is False


def test_unrelated_client_or_non_text_cannot_receive_exception():
    assert direct_inbound_reengagement_allowed(None, "Hola") is False
    assert direct_inbound_reengagement_allowed({}, "Hola") is False
    assert direct_inbound_reengagement_allowed(
        {"messages": [{"role": "user", "content": "Quiero vender"}]},
        "Hola",
    ) is False
    assert direct_inbound_reengagement_allowed(
        completed_search_lead(), "H" * 8001
    ) is False


def test_core_uses_inbound_reengagement_after_regular_qa_gate():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "chatbot" / "core.py").read_text(
        encoding="utf-8",
    )
    routine = src.split("async def process_user_message(", 1)[1].split(
        "def process_user_message_sync(", 1
    )[0]
    assert "if lead_must_not_be_contacted(lead_doc_full):" in routine
    assert "qa_completed_search_response_allowed(lead_doc_full, pilot_context)" in routine
    assert "direct_inbound_reengagement_allowed(lead_doc_full, original_message)" in routine
    assert "if await _run_sync(obtener_bot_pausado, phone):" in routine
