"""Fail-closed historical inquiry eligibility regression for V2 campaigns.

No network, no real phone contact, no production DB writes. A synthetic contact
and mongomock documents exercise the exact client path; internal tests are separate.
"""
from datetime import datetime, timezone

import mongomock
import pytest
from bson import ObjectId

from chatbot import price_drop_leads_v2 as v2

PHONE = "+56930001111"
CID = "pilot_6436_hot_20261106"
LID = "690000000000000000000555"
NOW = datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch):
    for name in (v2.CONTEXT_FLAG, v2.SEND_FLAG, v2.APPROVED_ID_FLAG):
        monkeypatch.delenv(name, raising=False)
    d = mongomock.MongoClient()["URLS"]
    d["universo_cartera_prop360"].insert_one({
        "codigo": "6436", "disponible_prop360": True,
        "estado": {"oficina": "PROCASA SUCRE"},
        "tipo_operacion": {"venta": True, "precio_venta": {"precio_uf": 2690}},
        "publicaciones": {"aliases": [{
            "external_id": "MLC-3776431868", "resolvable": True,
            "publication_status": "historical",
        }]},
    })
    d["universo_cartera_prop360_historial"].insert_one({
        "codigo": "6436", "campo": "precio_publicado",
        "fecha": "2026-10-14T11:52:43+00:00",
        "moneda_anterior": "UF", "moneda_nueva": "UF",
        "monto_anterior": 2790, "monto_nuevo": 2690,
    })
    d["leads"].insert_one({
        "_id": ObjectId(LID), "phone": PHONE,
        "lead_temperature_effective": "HOT", "lead_temperature": "HOT",
        "pipeline_stage": "CONTACTED",
        "messages": [{
            "role": "user",
            "content": "Hola, consulto MLC-3776431868 de San Miguel",
            "timestamp": "2026-07-16T14:20:55-04:00",
        }],
    })
    return d


def args():
    return {
        "lead_id": LID, "phone": PHONE, "campaign_id": CID,
        "property_code": "6436", "old_price_uf": 2790,
        "new_price_uf": 2690, "test_mode": False,
        "contact_basis_reviewed": True,
        "contact_basis_type": v2.DIRECT_RESPONSE_BASIS_TYPE,
        "contact_basis": "Consulta previa verificada sobre la misma propiedad",
    }


@pytest.mark.parametrize("temperature", [None, "", "COLD", "NORMAL"])
def test_real_campaign_can_recover_nonhot_historic_inquiry(db, temperature):
    db["leads"].update_one({"_id": ObjectId(LID)}, {
        "$set": {"lead_temperature_effective": temperature,
                 "lead_temperature": "HOT", "lifecycle.hot_since": "2026-07-16T14:25:56-04:00",
                 "last_intent": "ASK_VISIT"},
    })
    preview = v2.preview_one(db, **args())
    assert preview["property_code"] == "6436"
    assert preview["inquiry_evidence_at"] < preview["price_drop_at"]
    assert not db["leads"].find_one({"_id": ObjectId(LID)}).get("price_drop_v2")


@pytest.mark.parametrize("stage_field", ["pipeline_stage", "stage"])
@pytest.mark.parametrize("closed_stage", ["CLOSED_LOST", "CLOSED_WON"])
def test_closed_hot_lead_is_not_contacted(db, stage_field, closed_stage):
    db["leads"].update_one({"_id": ObjectId(LID)}, {
        "$set": {stage_field: closed_stage},
    })
    with pytest.raises(v2.PilotBlocked, match="lead_closed_pipeline_stage"):
        v2.preview_one(db, **args())


def test_open_hot_with_verified_prior_inquiry_passes_preview_without_write(db):
    res = v2.preview_one(db, **args())
    assert res["property_code"] == "6436"
    assert res["test_mode"] is False
    assert "UF 2.690" in res["message"]
    assert db["leads"].find_one({"_id": ObjectId(LID)}).get("price_drop_v2") is None


def test_hot_to_cold_before_prepare_can_still_prepare_historical_recovery(db, monkeypatch):
    original = v2.preview_one
    def goes_cold_after_preview(*a, **kw):
        result = original(*a, **kw)
        db["leads"].update_one({"_id": ObjectId(LID)},
                               {"$set": {"lead_temperature_effective": "COLD"}})
        return result
    monkeypatch.setattr(v2, "preview_one", goes_cold_after_preview)
    assert v2.prepare_one(db, now=NOW, **args())["status"] == "PREPARED"
    assert db["leads"].find_one({"_id": ObjectId(LID)})["price_drop_v2"]["campaigns"][0]["status"] == "PREPARED"


def test_hot_to_cold_before_approval_still_allows_approval(db):
    assert v2.prepare_one(db, now=NOW, **args())["status"] == "PREPARED"
    db["leads"].update_one({"_id": ObjectId(LID)},
                           {"$set": {"lead_temperature_effective": "COLD"}})
    assert v2.approve_one(db, lead_id=LID, campaign_id=CID,
                          operator="Supervisor", now=NOW)["status"] == "APPROVED"
    assert db["leads"].find_one()["price_drop_v2"]["campaigns"][0]["status"] == "APPROVED"


def test_cold_real_send_remains_locked_until_separate_pilot_approval(db, monkeypatch):
    v2.prepare_one(db, now=NOW, **args())
    v2.approve_one(db, lead_id=LID, campaign_id=CID,
                   operator="Supervisor", now=NOW)
    db["leads"].update_one({"_id": ObjectId(LID)},
                           {"$set": {"lead_temperature_effective": "COLD"}})
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CID)
    calls = []
    outcome = v2.send_one(db, lead_id=LID, campaign_id=CID,
                          sender=lambda *a: calls.append(a), now=NOW)
    assert outcome["status"] == "REAL_SEND_LOCKED_PENDING_ACCEPTANCE"
    assert calls == []
    assert db["leads"].find_one()["price_drop_v2"]["campaigns"][0]["status"] == "APPROVED"


def test_real_hot_send_still_requires_separate_pilot_approval(db, monkeypatch):
    v2.prepare_one(db, now=NOW, **args())
    v2.approve_one(db, lead_id=LID, campaign_id=CID,
                   operator="Supervisor", now=NOW)
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CID)
    calls = []
    result = v2.send_one(db, lead_id=LID, campaign_id=CID,
                         sender=lambda *a: calls.append(a), now=NOW)
    assert result["status"] == "REAL_SEND_LOCKED_PENDING_ACCEPTANCE"
    assert calls == []
