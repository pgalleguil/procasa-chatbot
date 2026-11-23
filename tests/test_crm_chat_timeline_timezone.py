"""CRM chat chronology must display explicit instants in Santiago time."""

from datetime import datetime

from api_crm import process_chat_timeline


def test_explicit_utc_and_chilean_offsets_render_identically():
    rows = process_chat_timeline([
        {"role": "user", "timestamp": "2026-11-18T12:30:00+00:00"},
        {"role": "assistant", "timestamp": "2026-11-18T12:30:00Z"},
        {"role": "agent", "timestamp": "2026-11-18T09:30:00-03:00"},
    ])
    assert len(rows) == 3
    assert all(x["timestamp"].strftime("%Y-%m-%d %H:%M") == "2026-11-18 09:30" for x in rows)
    assert all(x["timestamp"].utcoffset().total_seconds() == -10800 for x in rows)


def test_legacy_unzoned_string_is_not_silently_interpreted_as_utc():
    row = process_chat_timeline([{"timestamp": "2026-11-18T09:30:00.000"}])[0]
    assert row["timestamp"].hour == 9
    assert row["timestamp"].tzinfo is None


def test_bson_naive_datetime_is_utc_for_crm_display():
    row = process_chat_timeline([{"timestamp": datetime(2026, 11, 18, 12, 30)}])[0]
    assert row["timestamp"].strftime("%H:%M") == "09:30"
    assert row["timestamp"].tzinfo is not None


def test_crm_timeline_separates_chatbot_customer_and_named_executive():
    rows = process_chat_timeline([
        {
            "role": "user", "actor_type": "customer",
            "channel": "executive_whatsapp", "source": "executive_whatsapp",
            "content": "Quisiera visitar", "timestamp": "2026-11-23T19:12:06Z",
        },
        {
            "role": "agent", "actor_type": "human_agent",
            "actor_id": "exec-123", "source": "whatsapp_direct",
            "channel": "executive_whatsapp", "content": "Soy Hernán",
            "timestamp": "2026-11-23T18:59:30Z",
        },
        {
            "role": "assistant", "actor_type": "bot",
            "content": "Hola, soy el chatbot", "timestamp": "2026-11-23T18:56:42Z",
        },
        {
            "role": "user", "content": "Hola", "timestamp": "2026-11-23T18:56:24Z",
        },
    ], {"exec-123": "Hernán Castro"})
    assert [row["author_type"] for row in rows] == [
        "customer", "bot", "executive", "customer"
    ]
    assert [row["author_label"] for row in rows] == [
        "Cliente", "Chatbot PROCASA", "Hernán Castro", "Cliente"
    ]
    assert rows[2]["role"] == "chat-executive"
    assert rows[2]["channel_label"] == "WhatsApp del ejecutivo"
    assert rows[1]["channel_label"] == "Chatbot de oficina"
    assert all(row["timestamp"].strftime("%d/%m/%Y") == "23/11/2026" for row in rows)
    assert [row["timestamp"].strftime("%H:%M:%S") for row in rows] == [
        "15:56:24", "15:56:42", "15:59:30", "16:12:06"
    ]


def test_same_customer_text_in_two_chats_is_not_hidden_as_a_duplicate():
    text = "Donde se encuentra el galpón?"
    rows = process_chat_timeline([
        {"role": "user", "content": text, "timestamp": "2026-11-23T19:12:06Z",
         "source": "executive_whatsapp", "channel": "executive_whatsapp"},
        {"role": "user", "content": text, "timestamp": "2026-11-23T19:06:48Z"},
    ])
    assert len(rows) == 2
    assert [row["channel_label"] for row in rows] == [
        "Canal sin especificar", "WhatsApp del ejecutivo"
    ]
    assert rows[0]["timestamp"] < rows[1]["timestamp"]


def test_reassignment_does_not_attribute_old_agent_to_current_owner():
    rows = process_chat_timeline([
        {"role": "agent", "actor_type": "human_agent", "actor_id": "old-id",
         "timestamp": "2026-11-23T10:00:00-03:00"},
    ], {"new-id": "Ejecutiva Nueva"})
    assert rows[0]["author_label"] == "Ejecutivo"
    assert rows[0]["author_label"] != "Ejecutiva Nueva"


def test_crm_api_and_executive_direct_messages_are_distinct():
    rows = process_chat_timeline([
        {"role": "agent", "actor_type": "human_agent", "source": "crm_manual_api",
         "timestamp": "2026-11-23T16:45:07Z"},
        {"role": "agent", "actor_type": "human_agent", "source": "whatsapp_direct",
         "channel": "executive_whatsapp", "timestamp": "2026-11-23T16:45:38Z"},
    ])
    assert [row["channel_label"] for row in rows] == [
        "Enviado desde CRM", "WhatsApp del ejecutivo"
    ]



def test_detail_template_parses_and_uses_explicit_actor_classification():
    from pathlib import Path
    from jinja2 import Environment

    template_source = Path("templates/crm_lead_detail.html").read_text(encoding="utf-8-sig")
    Environment().parse(template_source)
    assert "Ver conversación completa (chatbot, cliente y ejecutivo)" in template_source
    assert "msg.author_label" in template_source
    assert "msg.channel_label" in template_source
    assert "msg.timestamp.strftime('%d/%m/%Y %H:%M')" in template_source
    assert "chat-msg-executive" in template_source
