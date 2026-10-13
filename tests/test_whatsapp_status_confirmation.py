"""Regression tests distinguishing provider acceptance from delivery."""
import asyncio

from chatbot import whatsapp_client


class _Response:
    status_code = 200
    content = b"{}"

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def test_provider_status_read_requires_matching_recipient_evidence(monkeypatch):
    body = {
        "data": {
            "status": 4,
            "id": "88035997",
            "key": {"remoteJid": "56912348250@s.whatsapp.net"},
        }
    }
    monkeypatch.setattr(whatsapp_client.requests, "get", lambda *args, **kwargs: _Response(body))

    matched = whatsapp_client.get_whatsapp_message_status_sync(
        "88035997", expected_recipient="+56 9 1234 8250"
    )
    mismatched = whatsapp_client.get_whatsapp_message_status_sync(
        "88035997", expected_recipient="+56 9 1234 0000"
    )

    assert matched["delivery_status"] == "read"
    assert matched["provider_message_id_matches"] is True
    assert matched["recipient_matches"] is True
    assert mismatched["delivery_status"] == "read"
    assert mismatched["recipient_matches"] is False
    assert "56912348250" not in repr(matched)


def test_sent_is_not_treated_as_delivery_confirmation(monkeypatch):
    responses = iter([
        {"delivery_status": "sent", "recipient_matches": True},
        {"delivery_status": "delivered", "recipient_matches": True},
    ])

    async def get_status(*args, **kwargs):
        return next(responses)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(whatsapp_client, "get_whatsapp_message_status", get_status)
    monkeypatch.setattr(whatsapp_client.asyncio, "sleep", no_sleep)
    result = asyncio.run(whatsapp_client.wait_for_whatsapp_delivery(
        "wamid-1", timeout_seconds=1, expected_recipient="+56912348250"
    ))

    assert result["delivery_status"] == "delivered"
    assert result["recipient_matches"] is True


def test_read_receipt_is_terminal_only_for_the_expected_recipient(monkeypatch):
    body = {
        "data": {
            "status": 4,
            "id": "wamid-2",
            "key": {"remoteJid": "56912340000@s.whatsapp.net"},
        }
    }
    monkeypatch.setattr(whatsapp_client.requests, "get", lambda *args, **kwargs: _Response(body))
    status = whatsapp_client.get_whatsapp_message_status_sync(
        "wamid-2", expected_recipient="+56912348250"
    )
    assert status["delivery_status"] == "read"
    assert status["recipient_matches"] is False

    statuses = iter([
        {"delivery_status": "read", "recipient_matches": False},
        {"delivery_status": "read", "recipient_matches": True},
    ])

    async def get_status(*args, **kwargs):
        return next(statuses)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(whatsapp_client, "get_whatsapp_message_status", get_status)
    monkeypatch.setattr(whatsapp_client.asyncio, "sleep", no_sleep)
    verified = asyncio.run(whatsapp_client.wait_for_whatsapp_delivery(
        "wamid-2", timeout_seconds=1, expected_recipient="+56912348250"
    ))
    assert verified["delivery_status"] == "read"
    assert verified["recipient_matches"] is True
