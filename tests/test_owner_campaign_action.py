from __future__ import annotations

from pathlib import Path

import mongomock
import pytest

from campanas import handler
from campanas import owner_campaign_confirmation_page
from campanas import owner_campaign_live_events
from config import Config


def add_master_property(db, property_code, price_uf):
    from campanas import owner_campaign_test_runtime as runtime

    db[runtime.PROPERTY_COLLECTION].insert_one({
        "codigo": property_code,
        "tipo_operacion": {
            "venta": True, "arriendo": False,
            "precio_venta": {"precio_uf": price_uf},
            "precio_arriendo": {},
        },
    })


def test_revisar_ajuste_get_renders_decision_page(monkeypatch):
    campaign_id = "owner_price_sucre_wave2_20260930"
    property_code = "5748"
    recipient = "owner@example.com"
    db_client = mongomock.MongoClient()
    db = db_client[Config.DB_NAME]
    db["ajuste_precio"].insert_one({
        "_id": f"{campaign_id}:{property_code}",
        "campaign_id": campaign_id,
        "property_code": property_code,
        "owner_email": recipient,
        "send_status": "SENT",
        "operation": "VENTA",
        "property_type": "Casa",
        "commune": "Puente Alto",
        "current_price": 3500,
        "recommended_price": 3150,
        "recommended_adjustment_pct": 10,
        "authorization_status": "PENDING",
    })
    add_master_property(db, property_code, 3500)

    claims = {
        "campaign_id": campaign_id,
        "property_code": property_code,
        "recipient": recipient,
        "action": "aceptar_rebaja",
        "event_id": "test-event",
    }
    monkeypatch.setattr("pymongo.MongoClient", lambda _uri: db_client)
    monkeypatch.setattr(owner_campaign_live_events, "verify_live_token", lambda *_args, **_kwargs: claims)
    monkeypatch.setattr(owner_campaign_live_events, "persist_live_event", lambda *_args, **_kwargs: (None, True))
    monkeypatch.setattr(owner_campaign_live_events, "issue_attributed_followup_token", lambda *_args, **_kwargs: "p1.advisor")
    monkeypatch.setattr(owner_campaign_confirmation_page, "render_decision_page", lambda **_kwargs: "decision-page")

    response = handler._process_owner_campaign_action(
        email=recipient,
        accion="aceptar_rebaja",
        codigo=property_code,
        campana=campaign_id,
        token="p1.valid",
        method="GET",
    )

    assert response.status_code == 200
    assert response.body.decode() == "decision-page"


def test_price_authorization_stays_fail_closed_when_mongo_write_fails(monkeypatch):
    campaign_id = "owner_price_sucre_wave2_20260930"
    property_code = "5748"
    recipient = "owner@example.com"
    db_client = mongomock.MongoClient()
    db = db_client[Config.DB_NAME]
    db["ajuste_precio"].insert_one({
        "_id": f"{campaign_id}:{property_code}", "campaign_id": campaign_id,
        "property_code": property_code, "owner_email": recipient, "send_status": "SENT",
        "operation": "VENTA", "property_type": "Casa", "commune": "Puente Alto",
        "current_price": 3500, "recommended_price": 3150,
        "recommended_adjustment_pct": 10, "authorization_status": "PENDING",
    })
    add_master_property(db, property_code, 3500)
    claims = {"campaign_id": campaign_id, "property_code": property_code,
              "recipient": recipient, "action": "aceptar_rebaja", "event_id": "critical-write"}
    monkeypatch.setattr("pymongo.MongoClient", lambda _uri: db_client)
    monkeypatch.setattr(owner_campaign_live_events, "verify_live_token", lambda *_args, **_kwargs: claims)
    monkeypatch.setattr(owner_campaign_live_events, "issue_attributed_followup_token", lambda *_args, **_kwargs: "p1.advisor")

    def fail_authorization(_db, _claims, *, event, action, details=None):
        if event == "price_authorized":
            raise RuntimeError("temporary mongo outage")
        return {}, True

    monkeypatch.setattr(owner_campaign_live_events, "persist_live_event", fail_authorization)
    with pytest.raises(RuntimeError, match="temporary mongo outage"):
        handler._process_owner_campaign_action(
            email=recipient, accion="aceptar_rebaja", codigo=property_code,
            campana=campaign_id, token="p1.valid", method="POST",
            selected_adjustment_type="RECOMMENDED",
        )
    row = db["ajuste_precio"].find_one({"_id": f"{campaign_id}:{property_code}"})
    assert row["authorization_status"] == "PENDING"
    assert not any(event.get("event") == "price_authorized" for event in row.get("events", []))


def test_live_price_drift_blocks_get_and_post_authorization_without_recording(monkeypatch):
    campaign_id = "owner_price_sucre_wave2_20260930"
    property_code = "5748"
    recipient = "owner@example.com"
    db_client = mongomock.MongoClient()
    db = db_client[Config.DB_NAME]
    db["ajuste_precio"].insert_one({
        "_id": f"{campaign_id}:{property_code}", "campaign_id": campaign_id,
        "property_code": property_code, "owner_email": recipient, "send_status": "SENT",
        "operation": "VENTA", "current_price": 3500, "recommended_price": 3150,
        "recommended_adjustment_pct": 10, "authorization_status": "PENDING", "events": [],
    })
    add_master_property(db, property_code, 3600)
    claims = {"campaign_id": campaign_id, "property_code": property_code,
              "recipient": recipient, "action": "aceptar_rebaja", "event_id": "must-not-persist"}
    monkeypatch.setattr("pymongo.MongoClient", lambda _uri: db_client)
    monkeypatch.setattr(owner_campaign_live_events, "verify_live_token", lambda *_args, **_kwargs: claims)

    for method in ("GET", "POST"):
        response = handler._process_owner_campaign_action(
            email=recipient, accion="aceptar_rebaja", codigo=property_code,
            campana=campaign_id, token="p1.valid", method=method,
            selected_adjustment_type="RECOMMENDED",
        )
        assert response.status_code == 409
        assert "El precio vigente de la ficha" in response.body.decode()
    row = db["ajuste_precio"].find_one({"_id": f"{campaign_id}:{property_code}"})
    assert row["authorization_status"] == "PENDING"
    assert row["events"] == []


def test_owner_pages_use_the_crm_procasa_favicon():
    favicon = '/static/favicon_procasa_mark.png?v=1.0.13'
    for template_name in ("owner_campaign_monthly_portal.html", "owner_campaign_private.html"):
        template = Path("templates", template_name).read_text(encoding="utf-8")
        assert f'href="{favicon}"' in template

    decision_page = owner_campaign_confirmation_page._document("title", "subtitle", "content")
    assert f'href="{favicon}"' in decision_page
    assert Path("static/favicon_procasa_mark.png").is_file()
