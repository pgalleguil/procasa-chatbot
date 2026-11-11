"""Prevent undelivered chatbot replies to valid international WhatsApp senders.

No external network calls. In particular, preserve Chilean recipient semantics,
the provider confirmation contract and fail-closed invalid number handling.
"""
from unittest.mock import patch

import pytest

from chatbot import whatsapp_client as client


def test_explicit_international_e164_survives_outbound_normalization():
    assert client.normalize_whatsapp_recipient("+6281234567890") == "+6281234567890"
    assert client.provider_recipient("+6281234567890") == "6281234567890"
    assert client.mask_whatsapp_recipient("+6281234567890") == "internacional **** 7890"
    # Formatting of already qualified numbers is harmless.
    assert client.normalize_whatsapp_recipient("+62 812 3456 7890") == "+6281234567890"


def test_chilean_numbers_keep_existing_exact_behavior():
    assert client.normalize_whatsapp_recipient("912345678") == "+56912345678"
    assert client.normalize_whatsapp_recipient("+56912345678") == "+56912345678"
    assert client.provider_recipient("+56912345678") == "56912345678"
    assert client.mask_whatsapp_recipient("+56912345678") == "+56 9 **** 5678"
    # Existing provider JID passthrough remains unchanged.
    assert client.normalize_whatsapp_recipient("120363000000@g.us") == "120363000000@g.us"


@pytest.mark.parametrize("number", [
    "812345678",     # no international country code: never guess
    "+0123456789",   # E.164 country codes never begin with zero
    "+628abc7890",   # do not silently strip alphabetic characters
    "+621",          # too short
    "+62812345678901234",  # E.164 too long
])
def test_invalid_international_numbers_are_not_sent(number):
    with pytest.raises(ValueError):
        client.normalize_whatsapp_recipient(number)


@pytest.mark.asyncio
async def test_async_chatbot_sender_calls_provider_for_international_recipient(monkeypatch):
    calls = []

    async def no_throttle():
        return None

    class ProviderResponse:
        status_code = 200

        def json(self):
            return {"success": True, "data": {"messageId": "test-provider-id"}}

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"])
        return ProviderResponse()

    monkeypatch.setattr(client, "throttle_outgoing_message", no_throttle)
    monkeypatch.setattr(client.requests, "post", fake_post)
    result = await client.send_whatsapp_message_detailed(
        "+6281234567890", "Gracias por consultar"
    )
    assert result["success"] is True
    assert result["provider_message_id"] == "test-provider-id"
    assert result["delivery_status"] == "accepted"
    assert calls == [{"to": "6281234567890", "text": "Gracias por consultar"}]


@pytest.mark.asyncio
async def test_invalid_recipient_fails_before_provider_without_uncertain_delivery(monkeypatch):
    monkeypatch.setattr(client.requests, "post", lambda *_a, **_kw: pytest.fail("Provider called"))
    result = await client.send_whatsapp_message_detailed("+62badnumber", "Hola")
    assert result == {
        "success": False, "delivery_status": "rejected_invalid_recipient",
        "provider_message_id": None, "http_status": 422,
        "provider_called": False,
    }
    assert not result.get("provider_call_uncertain")


def test_sync_sender_matches_international_policy_and_no_provider_for_invalid(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"success": True, "msgId": "test-sync-id"}

    monkeypatch.setattr(client, "throttle_outgoing_message_sync", lambda: None)
    monkeypatch.setattr(client.requests, "post", lambda _url, **kw: (calls.append(kw["json"]), Response())[1])
    good = client.send_whatsapp_message_detailed_sync("+6281234567890", "Hola")
    assert good["success"] and good["provider_message_id"] == "test-sync-id"
    assert calls == [{"to": "6281234567890", "text": "Hola"}]
    bad = client.send_whatsapp_message_detailed_sync("+62badnumber", "Hola")
    assert bad["delivery_status"] == "rejected_invalid_recipient"
    assert bad["http_status"] == 422
    assert bad["provider_called"] is False
    assert len(calls) == 1

