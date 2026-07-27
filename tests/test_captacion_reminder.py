from datetime import datetime, timezone
from chatbot.captacion_reminder import (DOMAIN, MESSAGE_TYPE, RECIPIENT_ROLE,
                                        canonical_audit_note, has_degraded_unicode, property_summary, reminder_text)

def test_captacion_reminder_domain_is_explicit_and_independent():
    assert (DOMAIN, MESSAGE_TYPE, RECIPIENT_ROLE) == ("captacion_reminder", "scheduled_reminder", "executive")

def test_degraded_input_is_detected_before_delivery():
    good = "Natalia Mu" + chr(0x00f1) + "oz"
    assert has_degraded_unicode("Natalia Mu?oz")
    assert has_degraded_unicode("inter?s")
    assert not has_degraded_unicode(good, "inter" + chr(0x00e9) + "s", "pr" + chr(0x00f3) + "xima", "gesti" + chr(0x00f3) + "n")

def test_property_summary_formats_uf_without_unnecessary_decimals():
    separator = " " + chr(0x00b7) + " "
    assert property_summary({"codigo":"6132","tipo_propiedad":"casa","comuna":"Santiago",
                             "operacion":"venta","precio_uf":3393.0}) == separator.join(["Venta","6132","Casa","Santiago","3.393 UF"])
    assert property_summary({"codigo":"X","operacion":"arriendo","precio_uf":1713.8}) == separator.join(["Arriendo","X","1.713,8 UF"])

def test_reminder_message_keeps_utf8_state_note_and_absolute_url():
    enye, accent = chr(0x00f1), chr(0x00e9)
    contact = "Natalia Mu" + enye + "oz"
    note = contact + " " + chr(0x2014) + " Gesti" + chr(0x00f3) + "n pendiente: llamar para confirmar inter" + accent + "s y pr" + chr(0x00f3) + "xima visita."
    task = {"obj_id":"id-1","scheduled_at":datetime(2026,7,27,15,20,tzinfo=timezone.utc),"audit_note":note,"contact_name":contact}
    cap = {"codigo":"6132","tipo_propiedad":"casa","comuna":"Santiago","operacion":"venta","precio_uf":3393.0,"gestion":{"estado_captacion":"Sin respuesta","notas":[{"content":note}]}}
    text=reminder_text(task,cap)
    assert contact in text and "*Estado actual:* Sin respuesta" in text
    assert "inter" + accent + "s y pr" + chr(0x00f3) + "xima" in text
    assert "Venta " + chr(0x00b7) + " 6132 " + chr(0x00b7) + " Casa " + chr(0x00b7) + " Santiago " + chr(0x00b7) + " 3.393 UF" in text
    assert "https://procasa-chatbot-yr8d.onrender.com/captacion/id-1" in text


def test_audit_note_must_exist_in_canonical_captacion_history():
    task = {"audit_note": "Nota real"}
    assert canonical_audit_note(task, {"gestion": {"notas": [{"content": "Nota real"}]}}) == "Nota real"
    assert canonical_audit_note(task, {"gestion": {"notas": []}}) is None
