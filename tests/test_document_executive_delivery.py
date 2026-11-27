from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
def test_unhealthy_session_requires_office_contingency(state):
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


def test_unlinked_creator_has_office_contingency():
    from chatbot.document_executive_delivery import OfficeDocumentFallback
    users = Mock()
    users.find_one.side_effect = [
        {"username": "owner", "rol": "agente"},
        {"_id": "owner", "telefono": "+56912345678"},
    ]
    with patch("chatbot.document_executive_delivery._session") as lookup:
        with pytest.raises(OfficeDocumentFallback) as exc:
            resolve_document_sender({"usuarios": users}, {"created_by": "owner"}, "owner")
        assert exc.value.reason == "session_not_linked"
        lookup.assert_not_called()


def test_wrong_sender_blocks_executive_and_requires_office():
    from chatbot.crm_executive_outbound import OutboundBlocked
    from chatbot.document_executive_delivery import OfficeDocumentFallback
    users = Mock()
    users.find_one.side_effect = [
        {"username": "owner", "rol": "agente"},
        {"_id": "owner", "telefono": "+56912345678", "wasender": {"session_id": 123}},
    ]
    with patch("chatbot.document_executive_delivery._session", return_value={"user_id": "owner"}), \
         patch("chatbot.document_executive_delivery._session_status", return_value="connected"), \
         patch("chatbot.document_executive_delivery.verify_provider_sender_identity",
               side_effect=OutboundBlocked("sender_identity_mismatch")):
        with pytest.raises(HTTPException) as exc:
            resolve_document_sender({"usuarios": users}, {"created_by": "owner"}, "owner")
        assert isinstance(exc.value, OfficeDocumentFallback)
        assert exc.value.reason == "sender_identity_mismatch"


def test_disconnected_creator_has_office_contingency():
    from chatbot.document_executive_delivery import OfficeDocumentFallback
    users = Mock()
    users.find_one.side_effect = [
        {"username": "owner", "rol": "agente"},
        {"_id": "owner", "telefono": "+56912345678", "wasender": {"session_id": 123}},
    ]
    with patch("chatbot.document_executive_delivery._session", return_value={"user_id": "owner"}), \
         patch("chatbot.document_executive_delivery._session_status", return_value="disconnected"):
        with pytest.raises(OfficeDocumentFallback):
            resolve_document_sender({"usuarios": users}, {"created_by": "owner"}, "owner")


@pytest.mark.asyncio
@pytest.mark.parametrize("collection,preflight,executive_result,office_result,expected_calls,expected_transport,expected_state", [
    ("visitas", None, ("accepted", "exec-1", None), None, ["executive"], "executive_wasender_api", "accepted"),
    ("contracts", "session_not_linked", None, ("accepted", "office-1", None), ["office"], "office_wasender_api", "accepted"),
    ("visitas", "session_disconnected", None, ("accepted", "office-1", None), ["office"], "office_wasender_api", "accepted"),
    ("contracts", "sender_identity_mismatch", None, ("accepted", "office-1", None), ["office"], "office_wasender_api", "accepted"),
    ("visitas", None, ("rejected", None, "session_disconnected"), ("accepted", "office-1", None), ["executive", "office"], "office_wasender_api", "accepted"),
    ("contracts", None, ("delivery_unknown", None, "provider_transport_uncertain"), None, ["executive"], "executive_wasender_api", "delivery_unknown"),
    ("visitas", None, ("rejected", None, "recipient_not_registered"), None, ["executive"], "executive_wasender_api", "rejected"),
    ("contracts", "session_not_linked", None, None, [], "office_wasender_api", "rejected"),
    ("visitas", "session_unavailable", None, ("delivery_unknown", None, "provider_transport_uncertain"), ["office"], "office_wasender_api", "delivery_unknown"),
])
async def test_document_routing_and_admin_incident(collection, preflight, executive_result, office_result,
                                                    expected_calls, expected_transport, expected_state):
    from datetime import datetime, timezone
    from chatbot import document_executive_delivery as mod
    executive = {"user_id": "executive", "session_id": "123", "token": "exec-token"}
    office = {"user_id": "office", "session_id": "office", "token": "office-token"}
    docs = Mock()
    docs.update_one = AsyncMock(return_value=SimpleNamespace(modified_count=1))
    document = {"_id": "doc-1", "created_by": "owner", "status": "created", "security": {"token": None}}
    calls = []
    def dispatch(session, recipient, message):
        calls.append(session["user_id"])
        return executive_result if session["user_id"] == "executive" else office_result
    resolver = Mock(return_value=executive)
    if preflight:
        resolver.side_effect = mod.OfficeDocumentFallback(preflight)
    alert = AsyncMock()
    with patch("chatbot.storage.get_db", return_value={}), \
         patch.object(mod, "resolve_document_sender", resolver), \
         patch.object(mod, "office_session", return_value=office if office_result else None), \
         patch.object(mod, "dispatch_document", side_effect=dispatch), \
         patch.object(mod, "throttle_outgoing_message", AsyncMock()), \
         patch.object(mod, "alert_document_problem", alert):
        kwargs = dict(collection=collection, document=document, username="owner", phone="+56912345678",
                      message="link", token="token", expiry=datetime.now(timezone.utc),
                      action="document_sent", server_timestamp="now", ip="ip", ua="ua")
        if expected_state == "accepted":
            result = await mod.send_document_invitation({collection: docs}, **kwargs)
            assert result["transport"] == expected_transport
        else:
            with pytest.raises(HTTPException):
                await mod.send_document_invitation({collection: docs}, **kwargs)
    assert calls == expected_calls
    final = docs.update_one.call_args.args[1]["$set"]
    assert final["whatsapp_invitation.state"] == expected_state
    if expected_transport == "office_wasender_api" or expected_state != "accepted":
        alert.assert_awaited_once()
        assert alert.call_args.kwargs["transport"] == expected_transport
        assert alert.call_args.kwargs["outcome"] == expected_state
    else:
        alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_alert_has_no_customer_link_and_records_provider_id():
    from chatbot import document_executive_delivery as mod
    docs = Mock()
    docs.update_one = AsyncMock(return_value=SimpleNamespace(modified_count=1))
    office = {"user_id": "office", "session_id": "office", "token": "office-token"}
    sender = Mock(return_value=("accepted", "admin-1", None))
    with patch("chatbot.executive_session_admin_alerts._pablo_recipient", return_value="+56912345678"), \
         patch.object(mod, "office_session", return_value=office), \
         patch.object(mod, "dispatch_document", sender), \
         patch.object(mod, "throttle_outgoing_message", AsyncMock()):
        await mod.alert_document_problem(docs, {"_id": "doc"}, db={}, collection="contracts",
            document={"_id": "doc", "created_by": "Erika", "contract_code": "PROC-TEST"},
            reason="session_not_linked", transport="office_wasender_api", outcome="accepted", attempt="test")
    assert sender.call_args.args[1] == "+56912345678"
    assert "PROC-TEST" in sender.call_args.args[2]
    assert "Erika" in sender.call_args.args[2]
    assert "WhatsApp no vinculado" in sender.call_args.args[2]
    assert "https://" not in sender.call_args.args[2]
    assert docs.update_one.call_args.args[1]["$set"]["whatsapp_invitation.admin_alert.provider_message_id"] == "admin-1"
