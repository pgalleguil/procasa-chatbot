from pathlib import Path

import pytest

from api_crm import _history_display_body, _history_human_text


def test_verified_whatsapp_history_is_commercial():
    event = {"type": "SEND_WA_LEAD", "result": "MESSAGE_SENT_WAITING_RESPONSE",
             "meta": {"notes": "WhatsApp enviado y guardado; pendiente respuesta"}}
    assert _history_display_body(event, "Mensaje enviado") == (
        "WhatsApp enviado. A la espera de respuesta del cliente."
    )


@pytest.mark.parametrize("result,title", [
    ("EFFECTIVE_CONTACT", "Contactado"),
    ("VISIT_SCHEDULED", "Visita agendada"),
    ("NOT_INTERESTED", "No interesado"),
    ("INVALID_NUMBER", "Número inválido"),
    ("CLOSED_WON", "Cerrado ganado"),
    ("CLOSED_LOST", "Cerrado perdido"),
])
def test_history_does_not_repeat_result_or_expose_code(result, title):
    assert _history_display_body({"type": "CONTACT_RESULT", "result": result}, title) == ""


def test_preserve_executive_notes():
    event = {"type": "CONTACT_RESULT", "result": "FOLLOW_UP_REQUESTED",
             "meta": {"notes": "Cliente solicita revisar alternativas en Maipú."}}
    assert _history_display_body(event, "En seguimiento") == event["meta"]["notes"]


def test_state_change_is_translated():
    event = {"type": "STATUS_CHANGE", "meta": {
        "from": "NEW", "to": "CONTACTED", "reason": "auto_promotion_on_gestion"}}
    assert _history_display_body(event, "Cambio de Estado") == "Nuevo → Contactado"


def test_alert_reason_is_translated():
    assert _history_display_body({"type": "ALERT_SENT", "meta": {
        "reason": "near_critical_threshold"}}, "Alerta enviada") == (
            "Plazo de atención próximo a vencer.")


def test_unknown_internal_codes_are_not_rendered():
    assert _history_human_text("UNRECOGNIZED_INTERNAL_CODE") == ""
    assert _history_display_body({"type": "CONTACT_RESULT", "result": "UNKNOWN_CODE"},
                                 "Gestión registrada") == ""


def test_template_only_displays_human_description():
    source = Path("templates/crm_lead_detail.html").read_text(encoding="utf-8")
    timeline = source.split('id="timelineBox"', 1)[1].split("{% endfor %}", 1)[0]
    assert "{{ h.display_body }}" in timeline
    assert "{% if h.display_body %}" in timeline
    assert "h.result" not in timeline
    assert "h.notes" not in timeline
