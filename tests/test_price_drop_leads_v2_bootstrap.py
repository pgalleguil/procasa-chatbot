"""No-network acceptance tests for single-phone V2 startup."""
import mongomock
import pytest
from bson import ObjectId

from scripts import price_drop_leads_v2_bootstrap as boot
from chatbot import price_drop_leads_v2 as v2

CID = "test_leads_6436_20261106_9804"
PHONE = "+56983219804"


@pytest.fixture
def db(monkeypatch):
    for key in (boot.BOOT_MODE, boot.BOOT_ID, boot.BOOT_OPERATOR,
                v2.SEND_FLAG, v2.CONTEXT_FLAG, v2.APPROVED_ID_FLAG,
                v2.TEST_PHONE_FLAG):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(boot.BOOT_MODE, "preview")
    monkeypatch.setenv(boot.BOOT_ID, CID)
    monkeypatch.setenv(v2.TEST_PHONE_FLAG, PHONE)
    database = mongomock.MongoClient()["URLS"]
    database["leads"].insert_one({"_id": ObjectId("690000000000000000000777"),
                                  "phone": PHONE, "messages": []})
    database["universo_cartera_prop360"].insert_one({
        "codigo": "6436", "disponible_prop360": True,
        "estado": {"oficina": "PROCASA SUCRE"},
        "tipo_operacion": {"venta": True, "precio_venta": {"precio_uf": 2690}},
    })
    database["universo_cartera_prop360_historial"].insert_one({
        "codigo": "6436", "campo": "precio_publicado",
        "fecha": "2026-10-14T11:52:43+00:00",
        "moneda_anterior": "UF", "moneda_nueva": "UF",
        "monto_anterior": 2790, "monto_nuevo": 2690,
    })
    return database


def arm(monkeypatch):
    monkeypatch.setenv(boot.BOOT_MODE, "send")
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CID)
    monkeypatch.setenv(boot.BOOT_OPERATOR, "Authorized 9804 test")


def test_preview_has_no_write(db):
    assert boot.run_bootstrap(db)["status"] == "READY"
    assert "price_drop_v2" not in db["leads"].find_one({"phone": PHONE})
    assert db["leads"].count_documents({}) == 1


def test_disabled_and_unarmed_do_not_send(db, monkeypatch):
    monkeypatch.setenv(boot.BOOT_MODE, "off")
    assert boot.run_bootstrap(db)["status"] == "DISABLED"
    monkeypatch.setenv(boot.BOOT_MODE, "send")
    called = []
    assert boot.run_bootstrap(db, sender=lambda *args: called.append(args))["status"] == "BLOCKED"
    assert called == []
    assert "price_drop_v2" not in db["leads"].find_one({"phone": PHONE})


def test_armed_sends_exactly_once_in_existing_lead(db, monkeypatch):
    arm(monkeypatch)
    calls = []
    def sender(phone, message):
        calls.append((phone, message))
        return {"success": True, "provider_message_id": "test-6436-9804",
                "delivery_status": "accepted"}
    result = boot.run_bootstrap(db, sender=sender)
    assert result["status"] == "SENT"
    assert len(calls) == 1 and calls[0][0] == PHONE
    assert "UF 2.790" in calls[0][1] and "UF 2.690" in calls[0][1]
    assert boot.run_bootstrap(db, sender=sender)["status"] == "ALREADY_ATTEMPTED"
    assert len(calls) == 1
    lead = db["leads"].find_one({"phone": PHONE})
    assert lead["price_drop_v2"]["campaigns"][0]["status"] == "SENT"
    assert lead["price_drop_v2"]["campaigns"][0]["test_mode"] is True
    assert len(lead["messages"]) == 1
    assert set(db.list_collection_names()) == {
        "leads", "universo_cartera_prop360", "universo_cartera_prop360_historial"}


def test_wrong_test_phone_blocks(db, monkeypatch):
    monkeypatch.setenv(v2.TEST_PHONE_FLAG, "+56911112222")
    assert boot.run_bootstrap(db)["status"] == "BLOCKED"


def test_price_changed_blocks_without_write(db, monkeypatch):
    arm(monkeypatch)
    db["universo_cartera_prop360"].update_one(
        {"codigo": "6436"},
        {"$set": {"tipo_operacion.precio_venta.precio_uf": 2750}})
    called = []
    assert boot.run_bootstrap(db, sender=lambda *args: called.append(args))["status"] == "BLOCKED"
    assert called == []
    assert "price_drop_v2" not in db["leads"].find_one({"phone": PHONE})


def test_provider_timeout_quarantined_no_retry(db, monkeypatch):
    arm(monkeypatch)
    calls = []
    def sender(*args):
        calls.append(1)
        raise TimeoutError("provider may have accepted")
    assert boot.run_bootstrap(db, sender=sender)["status"] == "DELIVERY_UNKNOWN"
    assert boot.run_bootstrap(db, sender=sender)["status"] == "ALREADY_ATTEMPTED"
    assert len(calls) == 1


def test_duplicate_canonical_phone_blocks(db, monkeypatch):
    arm(monkeypatch)
    db["leads"].insert_one({"_id": ObjectId("690000000000000000000778"),
                            "phone": PHONE, "messages": []})
    assert boot.run_bootstrap(db)["status"] == "BLOCKED"
