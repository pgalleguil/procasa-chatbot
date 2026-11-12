"""Recover only genuine customer replies from a locally verified WhatsApp export.

Never contact WaSender, queue chatbot work, or manufacture provider IDs.
"""
from datetime import datetime, timezone
from pathlib import Path

import mongomock
import pytest
from bson import ObjectId

from scripts import recuperar_respuestas_piloto_export as recovery


def seed_db():
    db = mongomock.MongoClient()["URLS"]
    db["leads"].insert_one({
        "_id": ObjectId("6af1c681e6682d1a6e43ab7a"),
        "phone": "+56987654321",
        "messages": [
            {"role": "user", "content": "Necesito el departamento",
             "timestamp": "2026-07-15T12:00:00-04:00"},
            {"role": "assistant", "content": "Te escribimos por la rebaja",
             "timestamp": "2026-11-12T10:24:03-03:00",
             "provider_message_id": "out123"},
        ],
        "price_drop_v2": {"campaigns": [{
            "campaign_id": recovery.PILOT,
            "property_code": "6436",
            "status": "SENT",
            "currency": "UF",
            "provider_message_id": "out123",
            "sent_at": datetime(2026, 11, 12, 13, 24, 3, tzinfo=timezone.utc),
            "reply_count": 0,
        }]},
    })
    return db


def write_export(tmp_path: Path, *, format: str = "android"):
    path = tmp_path / "chat.txt"
    if format == "ios":
        path.write_text(
            "[12/11/2026, 10:15:03] PROCASA: Antes del envío\n"
            "[12/11/2026, 11:15:03] Cliente: Hola, ¿sigue disponible?\n"
            "[12/11/2026, 11:17:03] PROCASA: Respondemos pronto\n",
            encoding="utf-8",
        )
    else:
        path.write_text(
            "12/11/2026, 10:15 - PROCASA: Hola antes de enviar\n"
            "12/11/2026, 10:24 - PROCASA: Te aviso la rebaja\n"
            "12/11/2026, 11:15 - Cliente: Me interesa el departamento 6436\n"
            "También quiero saber los gastos comunes\n"
            "12/11/2026, 11:16 - PROCASA: Recibido\n",
            encoding="utf-8",
        )
    return path


def prepare(db, path, **kw):
    return recovery.prepare_recovery(
        db, code="6436", chat_phone="+56987654321",
        customer_sender="Cliente", export=path,
        now=datetime(2026, 11, 12, 18, tzinfo=timezone.utc),
        **kw,
    )


@pytest.mark.parametrize("format", ["android", "ios"])
def test_preview_selects_only_real_client_messages_after_send(tmp_path, format):
    db = seed_db()
    path = write_export(tmp_path, format=format)
    before = list(db["leads"].find())
    plan = prepare(db, path)
    assert plan["selected_count"] == 1
    assert len(plan["messages"]) == 1
    assert plan["messages"][0]["text"].startswith(
        "Me interesa" if format == "android" else "Hola,")
    assert plan["messages"][0]["recovery_id"].startswith("whatsapp_export:")
    assert list(db["leads"].find()) == before


def test_apply_is_idempotent_and_never_fabricates_provider_id(tmp_path):
    db = seed_db()
    file = write_export(tmp_path)
    plan = prepare(db, file)
    with pytest.raises(recovery.RecoveryBlocked, match="confirmation"):
        recovery.apply_recovery(
            db, plan, confirm="ENVIAR 3", backup_root=tmp_path / "backup")
    assert len(db["leads"].find_one()["messages"]) == 2
    r = recovery.apply_recovery(
        db, plan, confirm="RECUPERAR 6436", backup_root=tmp_path / "backup")
    assert r["status"] == "RECOVERED_PENDING_HUMAN_REVIEW"
    assert r["restored"] == 1 and r["provider_sends"] == r["chatbot_replays"] == 0
    doc = db["leads"].find_one()
    assert len(doc["messages"]) == 3
    last = doc["messages"][-1]
    assert last["role"] == "user"
    assert last["provider_message_id"] is None
    assert last["recovery_source"] == "whatsapp_export_operator_verified"
    camp = doc["price_drop_v2"]["campaigns"][0]
    assert camp["reply_count"] == 1
    assert camp["status"] == "RESPONDED"
    assert camp["recovery_review_status"] == "PENDING_HUMAN_REVIEW"
    assert doc["price_drop_v2"]["reply_events"][0]["campaign_id"] == recovery.PILOT
    assert len(list((tmp_path / "backup").glob("*.json"))) == 1
    again = prepare(db, file)
    assert not again["messages"]
    assert again["duplicate_count"] == 1
    rr = recovery.apply_recovery(
        db, again, confirm="RECUPERAR 6436", backup_root=tmp_path / "backup")
    assert rr["status"] == "NO_NEW_MESSAGES"
    assert len(db["leads"].find_one()["messages"]) == 3
    assert len(list((tmp_path / "backup").glob("*.json"))) == 1


def test_reject_wrong_phone_wrong_sender_or_wrong_property(tmp_path):
    db = seed_db()
    file = write_export(tmp_path)
    with pytest.raises(recovery.RecoveryBlocked, match="phone_does_not_match"):
        recovery.prepare_recovery(
            db, code="6436", chat_phone="+56911112222",
            customer_sender="Cliente", export=file,
        )
    with pytest.raises(recovery.RecoveryBlocked, match="sender_not_found"):
        recovery.prepare_recovery(
            db, code="6436", chat_phone="+56987654321",
            customer_sender="Desconocido", export=file,
        )
    with pytest.raises(recovery.RecoveryBlocked, match="pilot_lead_not_unique"):
        recovery.prepare_recovery(
            db, code="6377", chat_phone="+56987654321",
            customer_sender="Cliente", export=file,
        )


def test_no_automatic_send_or_create_when_no_post_campaign_reply(tmp_path):
    db = seed_db()
    file = tmp_path / "chat.txt"
    file.write_text("12/11/2026, 10:15 - Cliente: Hola antes del anuncio\n",
                    encoding="utf-8")
    plan = prepare(db, file)
    assert plan["messages"] == []
    r = recovery.apply_recovery(
        db, plan, confirm="RECUPERAR 6436", backup_root=tmp_path / "backup")
    assert r["restored"] == 0
    assert not (tmp_path / "backup").exists()


def test_explicit_stop_is_preserved_and_suppresses_campaigns(tmp_path):
    db = seed_db()
    file = tmp_path / "chat.txt"
    file.write_text(
        "12/11/2026, 11:15 - Cliente: No me escriban más\n"
        "12/11/2026, 11:18 - Cliente: Gracias\n",
        encoding="utf-8",
    )
    plan = prepare(db, file)
    assert len(plan["messages"]) == 2
    recovery.apply_recovery(
        db, plan, confirm="RECUPERAR 6436", backup_root=tmp_path / "backup")
    doc = db["leads"].find_one()
    camp = doc["price_drop_v2"]["campaigns"][0]
    assert camp["status"] == "OPTED_OUT"
    assert doc["price_drop_v2"]["opted_out"] is True
    assert doc["conversation_status"] == "STOPPED_BY_CLIENT"
    assert doc["conversation_status_reason"] == "explicit_opt_out"


def test_reject_zip_bomb_and_unsupported_export(tmp_path):
    file = tmp_path / "bad.json"
    file.write_text("{}")
    with pytest.raises(recovery.RecoveryBlocked, match="whatsapp_txt_or_zip"):
        recovery.parse_export(file)


def test_existing_webhook_message_not_duplicated(tmp_path):
    db = seed_db()
    path = write_export(tmp_path)
    d = db["leads"].find_one()
    db["leads"].update_one({"_id":d["_id"]}, {"$push":{"messages":{
        "role":"user", "content":"Me interesa el departamento 6436\nTambién quiero saber los gastos comunes",
        "timestamp":"2026-11-12T11:15:15-03:00",
    }}})
    plan = prepare(db,path)
    assert len(plan["messages"]) == 0
    assert plan["duplicate_count"] == 1
