"""Regression: missing-property admin notices do not require an executive cycle."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatbot.crm_assignment_integrity import prepare_pending_missing_property_admin_alert
from chatbot.missing_property_admin import (
    MISSING_PROPERTY_ADMIN_PHONE,
    format_missing_property_admin_alert,
    format_missing_property_admin_summary,
    is_missing_property_admin_alert,
)


PHONE = "+56946433269"
URL = "https://www.portalinmobiliario.com/MLC-2406218977-departamento-en-arriendo-de-1-dorm-en-santiago-_JM"
PAYLOAD = {
    "phone": PHONE,
    "lead_type": "MissingProperty",
    "assignment_type": "MISSING_PROPERTY",
    "target_phone": MISSING_PROPERTY_ADMIN_PHONE,
    "last_message": f"Hola, tengo preguntas: {URL}",
}


def test_admin_missing_property_notice_is_explicitly_scoped():
    assert is_missing_property_admin_alert({"lead_data": PAYLOAD}, MISSING_PROPERTY_ADMIN_PHONE)
    assert not is_missing_property_admin_alert(
        {"lead_data": {**PAYLOAD, "lead_type": "InteresVisita"}}, MISSING_PROPERTY_ADMIN_PHONE
    )
    assert not is_missing_property_admin_alert(
        {"lead_data": {**PAYLOAD, "assignment_type": "NORMAL"}}, MISSING_PROPERTY_ADMIN_PHONE
    )
    assert not is_missing_property_admin_alert({"lead_data": PAYLOAD}, "56911111111")


def test_missing_property_admin_can_receive_notice_without_executive_assignment(monkeypatch):
    from chatbot import crm_metrics
    lead = {
        "_id": "test-lead",
        "phone": PHONE,
        "assignment_type": "MISSING_PROPERTY",
        "prospecto": {"link_pendiente": True},
    }
    monkeypatch.setattr(
        crm_metrics, "resolve_canonical_lead",
        lambda db, phone: SimpleNamespace(status="found", lead=lead),
    )
    result = prepare_pending_missing_property_admin_alert(
        {}, lead_phone=PHONE, requested_target_phone=MISSING_PROPERTY_ADMIN_PHONE,
        lead_type="MissingProperty", assignment_type="MISSING_PROPERTY",
    )
    assert result["status"] == "ready"
    assert result["owner_phone"] == MISSING_PROPERTY_ADMIN_PHONE
    assert result["admin_only"] is True
    assert result["cycle_id"] == ""


def test_missing_property_admin_guard_rejects_stale_or_wrong_recipient(monkeypatch):
    from chatbot import crm_metrics
    monkeypatch.setattr(
        crm_metrics, "resolve_canonical_lead",
        lambda db, phone: SimpleNamespace(status="found", lead={
            "_id": "test-lead", "assignment_type": "NORMAL", "prospecto": {"link_pendiente": False},
        }),
    )
    args = dict(
        lead_phone=PHONE, requested_target_phone=MISSING_PROPERTY_ADMIN_PHONE,
        lead_type="MissingProperty", assignment_type="MISSING_PROPERTY",
    )
    assert prepare_pending_missing_property_admin_alert({}, **args)["reason"] == "property_no_longer_pending"
    assert prepare_pending_missing_property_admin_alert(
        {}, **{**args, "requested_target_phone": "56911111111"}
    )["reason"] == "invalid_admin_notice"
    assert prepare_pending_missing_property_admin_alert(
        {}, **{**args, "lead_type": "InteresVisita"}
    )["reason"] == "invalid_admin_notice"


def test_admin_message_includes_mlc_reference_and_original_url():
    message = format_missing_property_admin_alert(PAYLOAD)
    assert PHONE in message
    assert "MLC-2406218977" in message
    assert URL in message
    assert "No se asignó ejecutivo" in message
    grouped = format_missing_property_admin_summary([{"lead_data": PAYLOAD}])
    assert PHONE in grouped and URL in grouped


def test_background_worker_has_separate_admin_and_executive_queues():
    source = (Path(__file__).parents[1] / "webhook.py").read_text(encoding="utf-8")
    assert 'group_key = (target_phone, "missing_property_admin" if admin_only else "executive")' in source
    assert 'prepare_pending_missing_property_admin_alert(' in source
    assert 'prepare_pending_assignment_notification(' in source
