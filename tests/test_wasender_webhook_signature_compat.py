"""WaSender webhook signature regression tests.

Neither a missing nor an incorrect signature can enter the inbound path.
The official header format and legacy HMAC both remain authenticated.
"""
import hashlib
import hmac

import pytest

from chatbot.webhook_event_policy import (
    verify_wasender_webhook_signature,
    is_outbound_message_event,
)


SECRET = "test-customer-session-webhook-secret-not-real"
BODY = b'{"event":"messages.received","data":{"messages":{"key":{"id":"in-1","fromMe":false},"messageBody":"Hola"}}}'


@pytest.mark.parametrize("candidate", [
    SECRET,
    hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest(),
    "sha256=" + hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest(),
])
def test_signature_accepts_documented_secret_and_legacy_hmac(candidate):
    assert verify_wasender_webhook_signature(BODY, candidate, SECRET)


@pytest.mark.parametrize("candidate", [
    None,
    "",
    "wrong secret",
    "sha256=" + "0" * 64,
    hmac.new(SECRET.encode("utf-8"), b'altered body', hashlib.sha256).hexdigest(),
])
def test_signature_rejects_missing_incorrect_or_replayed_body(candidate):
    assert not verify_wasender_webhook_signature(BODY, candidate, SECRET)


def test_invalid_or_missing_secret_never_accepted():
    assert not verify_wasender_webhook_signature(BODY, SECRET, None)
    assert not verify_wasender_webhook_signature(BODY, SECRET, "")
    assert not verify_wasender_webhook_signature(b"", SECRET, None)


def test_tamper_hmac_detected_and_raw_secret_compatibility_is_bound_to_configured_secret():
    signed = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()
    assert not verify_wasender_webhook_signature(BODY + b" ", signed, SECRET)
    assert not verify_wasender_webhook_signature(BODY, SECRET, "different-secret")
    assert not verify_wasender_webhook_signature(BODY, signed, "different-secret")


def test_inbound_received_event_does_not_become_outbound_message():
    inbound = {"event": "messages.received",
               "data": {"messages": {"key": {"id": "in-1", "fromMe": False},
                                      "messageBody": "Hola"}}}
    assert not is_outbound_message_event(inbound)
    assert is_outbound_message_event({"event": "message.sent", "data": {"id": "out-1"}})
