from datetime import datetime, timezone
from chatbot.captacion_reminder import DOMAIN, MESSAGE_TYPE, RECIPIENT_ROLE, reminder_text

def test_captacion_reminder_domain_is_explicit_and_independent():
    assert (DOMAIN, MESSAGE_TYPE, RECIPIENT_ROLE) == ("captacion_reminder", "scheduled_reminder", "executive")

def test_reminder_message_keeps_utf8_state_note_and_absolute_url():
    task = {"obj_id": "id-1", "scheduled_at": datetime(2026, 7, 27, 15, 20, tzinfo=timezone.utc),
            "audit_note": "Natalia Mu\u00f1oz \u2014 Gesti\u00f3n pendiente: llamar para confirmar inter\u00e9s y pr\u00f3xima visita.",
            "contact_name": "Natalia Mu\u00f1oz"}
    captacion = {"details": {"publicador": "Ignorado"}, "codigo": "6826",
                 "gestion": {"estado": "Sin respuesta"}}
    content = reminder_text(task, captacion)
    assert "RECORDATORIO DE CAPTACI\u00d3N" in content
    assert "Natalia Mu\u00f1oz" in content and "Gesti\u00f3n pendiente" in content
    assert "*Estado actual:* Sin respuesta" in content
    assert "https://procasa-chatbot-yr8d.onrender.com/captacion/id-1" in content
    assert "\ufffd" not in content and "/captacion/id-1" != content.splitlines()[-2]
