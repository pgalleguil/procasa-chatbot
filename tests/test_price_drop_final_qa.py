"""Regression: rerun only a completed test, preserve latest context and duplicate safety."""
from datetime import datetime, timedelta, timezone
from bson import ObjectId
import mongomock
from chatbot import price_drop_leads_v2 as v2


def test_second_qa_records_separate_send_and_reply(monkeypatch):
    phone = "+56930001111"
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    db = mongomock.MongoClient()["URLS"]
    lead_id = ObjectId("690000000000000000000555")
    db["leads"].insert_one({"_id": lead_id, "phone": phone,
                            "price_drop_v2": {"campaigns": []}})
    db["universo_cartera_prop360"].insert_one({
        "codigo": "6436", "disponible_prop360": True,
        "estado": {"oficina": "PROCASA SUCRE"},
        "tipo_operacion": {"venta": True, "precio_venta": {"precio_uf": 2690}}})
    db["universo_cartera_prop360_historial"].insert_one({
        "codigo": "6436", "campo": "precio_publicado",
        "fecha": "2026-10-14T11:52:43+00:00", "moneda_anterior": "UF",
        "moneda_nueva": "UF", "monto_anterior": 2790, "monto_nuevo": 2690})
    monkeypatch.setenv(v2.TEST_PHONE_FLAG, phone)
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    calls = []
    for i in range(2):
        cid = "test_leads_6436_qa_" + str(i)
        when = now + timedelta(minutes=3*i)
        v2.prepare_one(db, lead_id=str(lead_id), phone=phone, campaign_id=cid,
                       property_code="6436", old_price_uf=2790, new_price_uf=2690,
                       test_mode=True, now=when)
        v2.approve_one(db, lead_id=str(lead_id), campaign_id=cid,
                       operator="QA", now=when)
        monkeypatch.setenv(v2.APPROVED_ID_FLAG, cid)
        sent = v2.send_one(db, lead_id=str(lead_id), campaign_id=cid, now=when,
                           sender=lambda p,m: (
                               calls.append((p,m)) or
                               {"success":True,"provider_message_id":"out-"+cid}))
        assert sent["status"] == "SENT"
    ctx = v2.active_context(db, phone=phone, message="me interesa",
                            now=now+timedelta(minutes=4))
    assert ctx["campaign_id"] == "test_leads_6436_qa_1"
    assert v2.record_inbound(db, phone=phone, text="me interesa",
                             provider_message_id="in-last",
                             now=now+timedelta(minutes=4))["status"] == "ATTRIBUTED"
    campaigns = db["leads"].find_one({"_id": lead_id})["price_drop_v2"]["campaigns"]
    assert len(campaigns) == 2 and campaigns[0]["reply_count"] == 0
    assert campaigns[1]["reply_count"] == 1 and len(calls) == 2
    assert campaigns[1]["notifications"]["property_executive"]["status"] == "FROZEN_ADMIN_ONLY"
