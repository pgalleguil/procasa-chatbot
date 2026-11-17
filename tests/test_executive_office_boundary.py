"""Passive capture must not change office responses or campaign eligibility."""
from datetime import datetime, timezone

from chatbot.message_scope import office_messages
from chatbot import price_drop_leads_v2 as campaigns
import pytest


def executive(text, role='user'):
    return dict(role=role, actor_type='customer' if role == 'user' else 'human_agent',
                channel='executive_whatsapp', content=text,
                timestamp=datetime.now(timezone.utc).isoformat())


def test_passive_stop_does_not_enforce_campaign_optout():
    assert campaigns._contact_suppression_reasons({'messages': [executive('No me escriban más')]}) == set()


def test_office_stop_still_enforces_existing_policy():
    message = executive('No me escriban más')
    message.pop('channel')
    assert campaigns._contact_suppression_reasons({'messages': [message]}) == {'explicit_opt_out'}


def test_executive_inbound_does_not_open_office_cloud_window(monkeypatch):
    monkeypatch.setenv('PRICE_DROP_WHATSAPP_CHANNEL_MODE', 'official')
    result = campaigns._text_sender_window_blocker({'messages': [executive('Hola')]}, datetime.now(timezone.utc))
    assert result == 'whatsapp_24_hour_window_expired_template_required'


def test_office_policy_has_same_state_with_passive_evidence():
    from chatbot import conversation_policy
    if not hasattr(conversation_policy, "build_property_search_response"):
        pytest.skip("main baseline: phase3 imports missing build_property_search_response")
    from chatbot.phase3_conversation import build_conversation_state
    office = [{'role': 'user', 'content': 'Quiero información'}]
    expected = build_conversation_state({}, office)
    actual = build_conversation_state({}, office + [executive('Quiero agendar una visita')])
    assert actual == expected


def test_scope_preserves_legacy_office_history():
    original = [{'role': 'user', 'content': 'Hola'}, executive('No me contacten más'), None]
    assert office_messages(original) == [original[0]]


def test_office_history_reader_excludes_passive_channel(monkeypatch):
    from chatbot import storage
    class Collection:
        def find_one(self, *_):
            return {'messages': [{'role': 'user', 'content': str(i)} for i in range(60)] + [executive('No me contacten más')]}
    monkeypatch.setattr(storage, 'get_db', lambda: {'leads': Collection()})
    history = storage.obtener_conversacion('+56911112222')
    assert len(history) == 50
    assert history[0]['content'] == '10'
    assert history[-1]['content'] == '59'

def test_durable_executive_optout_blocks_campaigns_without_reading_passive_chat():
    from chatbot.price_drop_campaign_classifier import lead_must_not_be_contacted
    doc = {"contact_restrictions": {"marketing_opt_out": True},
           "messages": [executive("Hola, quiero consultar por una casa")]}
    assert lead_must_not_be_contacted(doc)
    assert campaigns._contact_suppression_reasons(doc) == {"explicit_opt_out"}
    # The customer may still initiate a new service inquiry; no CRM pause
    # or STOPPED_BY_CLIENT state is written by executive intake.
    assert "conversation_status" not in doc


def test_legacy_campaign_flags_remain_sufficient_even_without_new_metadata():
    from chatbot.price_drop_campaign_classifier import lead_must_not_be_contacted
    for key in ("price_drop_v2", "price_drop_campaign_v1"):
        assert lead_must_not_be_contacted({key: {"opted_out": True}})
        assert campaigns._contact_suppression_reasons({key: {"opted_out": True}}) == {"explicit_opt_out"}
