from pathlib import Path

from chatbot.lead_router import (
    build_crm_lead_url,
    format_summary_whatsapp_template,
    format_whatsapp_template,
)


def test_hot_lead_url_targets_specific_phone_and_property():
    url = build_crm_lead_url(
        {"phone": "+56 9 1234 5678", "property_code": "ABC 123"}
    )
    assert url.endswith("/crm/lead/56912345678?codigo=ABC+123")


def test_individual_hot_message_contains_direct_link_not_crm_root():
    message = format_whatsapp_template(
        {"phone": "+56912345678", "property_code": "7788", "last_message": "Quiero visitar"},
        "Erika Garrido",
        "7788",
    )
    assert "/crm/lead/56912345678?codigo=7788" in message
    assert "Ver y Gestionar en CRM" in message
    assert "onrender.com/\n\n" not in message


def test_summary_contains_one_direct_link_per_hot_lead():
    message = format_summary_whatsapp_template(
        [
            {"lead_data": {"phone": "+56911111111", "property_code": "100"}},
            {"lead_data": {"phone": "+56922222222", "property_code": "200"}},
        ],
        "Erika Garrido",
    )
    assert "/crm/lead/56911111111?codigo=100" in message
    assert "/crm/lead/56922222222?codigo=200" in message
    assert "/crm?temperatura=HOT" in message


def test_missing_phone_falls_back_to_hot_queue_not_generic_home():
    url = build_crm_lead_url({"phone": "", "property_code": "100"})
    assert url.endswith("/crm?temperatura=HOT")


def test_existing_auth_flow_preserves_requested_lead_path():
    source = Path("webhook.py").read_text(encoding="utf-8")
    assert 'requested_url = request.url.path' in source
    assert 'response.set_cookie("login_next", requested_url' in source
    assert 'target_url = _safe_login_next(request.cookies.get("login_next"))' in source
