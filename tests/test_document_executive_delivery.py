from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException

from chatbot.document_executive_delivery import (
    dispatch_document, document_recipient, resolve_document_sender,
)


@pytest.mark.parametrize("raw, expected", [
    ("9 1234 5678", "+56912345678"),
    ("+56 9 1234 5678", "+56912345678"),
    ("+1 202 555 0101", "+12025550101"),
])
def test_document_phone(raw, expected):
    assert document_recipient(raw) == expected


@pytest.mark.parametrize("raw", ["", "123", "123@g.us", "+56212345678"])
def test_reject_invalid_recipient(raw):
    with pytest.raises(HTTPException):
        document_recipient(raw)


def test_only_recorded_creator_session_can_send():
    users = Mock()
    users.find_one.side_effect = [
        {"username": "admin", "rol": "admin"},
        {"_id": "owner", "telefono": "+56912345678", "wasender": {"session_id": 123}},
    ]
    session = {"user_id": "owner", "session_id": "123", "token": "test"}
    with patch("chatbot.document_executive_delivery._session", return_value=session) as lookup, \
         patch("chatbot.document_executive_delivery._session_status", return_value="connected"), \
         patch("chatbot.document_executive_delivery.verify_provider_sender_identity") as verify:
        assert resolve_document_sender({"usuarios": users}, {"created_by": "owner"}, "admin") == session
        lookup.assert_called_once_with({"usuarios": users}, "123")
        verify.assert_called_once_with(session, expected_phone="+56912345678", force=True)


def test_non_owner_agent_cannot_send():
    users = Mock()
    users.find_one.return_value = {"username": "other", "rol": "agente"}
    with pytest.raises(HTTPException) as exc:
        resolve_document_sender({"usuarios": users}, {"created_by": "owner"}, "other")
    assert exc.value.status_code == 403


@pytest.mark.parametrize("state", ["disconnected", "unavailable"])
def test_no_office_fallback(state):
    users = Mock()
    users.find_one.side_effect = [
        {"username": "owner", "rol": "agente"},
        {"_id": "owner", "telefono": "+56912345678", "wasender": {"session_id": 123}},
    ]
    with patch("chatbot.document_executive_delivery._session", return_value={"user_id": "owner"}), \
         patch("chatbot.document_executive_delivery._session_status", return_value=state):
        with pytest.raises(HTTPException) as exc:
            resolve_document_sender({"usuarios": users}, {"created_by": "owner"}, "owner")
    assert exc.value.status_code == 409


@pytest.mark.parametrize("status, body, expected", [
    (200, {"success": True, "data": {"id": "provider-1"}}, ("accepted", "provider-1", None)),
    (200, {"success": True}, ("delivery_unknown", None, "provider_id_missing")),
    (500, {}, ("delivery_unknown", None, "provider_response_unverifiable")),
    (401, {}, ("rejected", None, "authentication")),
])
def test_provider_result_is_not_assumed_sent(status, body, expected):
    post = Mock(return_value=SimpleNamespace(status_code=status, json=lambda: body))
    assert dispatch_document({"token": "owner-token"}, "+56912345678", "link", post=post) == expected
    assert post.call_count == 1
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer owner-token"


def test_network_error_does_not_retry():
    post = Mock(side_effect=TimeoutError)
    result = dispatch_document({"token": "owner-token"}, "+56912345678", "link", post=post)
    assert result[0] == "delivery_unknown"
    assert post.call_count == 1
