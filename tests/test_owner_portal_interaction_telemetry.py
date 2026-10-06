from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from campanas.owner_campaign_live_events import (
    issue_live_token,
    persist_live_event,
)
from config import Config
from owner_portal.router import router


CAMPAIGN = "owner_price_sucre_wave2_20260930"
CODE = "17005"
EMAIL = "owner@example.com"


def make_db():
    db = mongomock.MongoClient()["telemetry_test"]
    db[Config.COLLECTION_CAMPANAS_LOG].insert_one({
        "_id": f"{CAMPAIGN}:{CODE}", "campaign_id": CAMPAIGN,
        "property_code": CODE, "owner_email": EMAIL, "authorization_status": "PENDING",
        "events": [],
    })
    return db


def make_client(monkeypatch, db):
    monkeypatch.setattr("owner_portal.router.get_db", lambda: db)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def interaction_token(monkeypatch, *, source="EMAIL"):
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "telemetry-test-secret")
    return issue_live_token(
        campaign_id=CAMPAIGN, property_code=CODE, action="owner_portal_interaction",
        recipient=EMAIL, expires_at=int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        source=source, interaction_surface="OWNER_PORTAL", report_period="2026-10",
        snapshot_hash="snapshot-hash-10",
    )


def event_payload(token, event, *, section, control, **extra):
    return {
        "token": token, "event": event, "client_event_id": str(uuid4()),
        "session_id": str(uuid4()), "section_id": section, "control_id": control,
        **extra,
    }


def test_interaction_endpoint_persists_allowlisted_events_and_month_context(monkeypatch):
    db = make_db()
    client = make_client(monkeypatch, db)
    token = interaction_token(monkeypatch)
    cases = [
        ("section_expanded", "appraisal_details", "appraisal_details_toggle", {"expanded": True}),
        ("section_expanded", "market_context_details", "market_context_toggle", {"expanded": True}),
        ("section_expanded", "recommendation_details", "recommendation_details_toggle", {"expanded": True}),
        ("price_simulation_changed", "price_simulation", "price_simulation_adjusted", {
            "previous_state": "current", "target_state": "adjusted", "selected_adjustment_pct": 9,
        }),
        ("price_simulation_changed", "price_simulation", "price_simulation_current", {
            "previous_state": "adjusted", "target_state": "current", "selected_adjustment_pct": 9,
        }),
        ("publication_link_clicked", "commercial_activity_publications", "publication_link", {
            "external_portal": "Yapo",
        }),
    ]
    for event, section, control, extra in cases:
        response = client.post("/owner-portal/interaction", json=event_payload(
            token, event, section=section, control=control, **extra,
        ))
        assert response.status_code == 200

    events = db[Config.COLLECTION_CAMPANAS_LOG].find_one({"_id": f"{CAMPAIGN}:{CODE}"})["events"]
    assert [item["event"] for item in events] == [case[0] for case in cases]
    assert all(item["interaction_surface"] == "OWNER_PORTAL" for item in events)
    assert all(item["interaction_channel"] == "EMAIL" for item in events)
    assert all(item["report_period"] == "2026-10" for item in events)
    assert all(item["snapshot_hash"] == "snapshot-hash-10" for item in events)
    assert events[0]["section_id"] == "appraisal_details"
    assert events[1]["section_id"] == "market_context_details"
    assert events[2]["section_id"] == "recommendation_details"
    assert (events[3]["previous_state"], events[3]["target_state"]) == ("current", "adjusted")
    assert (events[4]["previous_state"], events[4]["target_state"]) == ("adjusted", "current")
    assert events[3]["selected_adjustment_pct"] == 9
    assert events[5]["external_portal"] == "Yapo"


def test_interaction_idempotency_retries_once_but_distinct_clicks_both_persist(monkeypatch):
    db = make_db()
    client = make_client(monkeypatch, db)
    token = interaction_token(monkeypatch)
    payload = event_payload(token, "section_expanded", section="appraisal_details",
                            control="appraisal_details_toggle", expanded=True)
    assert client.post("/owner-portal/interaction", json=payload).status_code == 200
    assert client.post("/owner-portal/interaction", json=payload).status_code == 200
    second_click = {**payload, "client_event_id": str(uuid4())}
    assert client.post("/owner-portal/interaction", json=second_click).status_code == 200
    events = db[Config.COLLECTION_CAMPANAS_LOG].find_one({"_id": f"{CAMPAIGN}:{CODE}"})["events"]
    assert len(events) == 2
    assert len({item["event_id"] for item in events}) == 2


@pytest.mark.parametrize("payload_change", [
    {"event": "arbitrary_event"},
    {"extra": "unexpected"},
    {"section_id": "invented_section"},
    {"external_portal": "https://private.example/listing"},
])
def test_interaction_endpoint_rejects_unknown_or_unexpected_payload(monkeypatch, payload_change):
    db = make_db()
    client = make_client(monkeypatch, db)
    token = interaction_token(monkeypatch)
    valid = event_payload(token, "publication_link_clicked", section="commercial_activity_publications",
                          control="publication_link", external_portal="Yapo")
    response = client.post("/owner-portal/interaction", json={**valid, **payload_change})
    assert response.status_code == 400
    assert db[Config.COLLECTION_CAMPANAS_LOG].find_one({"_id": f"{CAMPAIGN}:{CODE}"})["events"] == []


@pytest.mark.parametrize("document_type,section,control", [
    ("INDIVIDUAL_APPRAISAL", "appraisal_report", "individual_appraisal_report"),
    ("COMMUNAL_MARKET_REPORT", "communal_market_report", "communal_market_report"),
])
def test_report_event_persists_document_type_and_context(monkeypatch, document_type, section, control):
    from types import SimpleNamespace
    import campanas.private_report as private_report
    from owner_portal.monthly import owner_property_portal_id

    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "telemetry-test-secret")
    mongo_client = mongomock.MongoClient()
    db = mongo_client[Config.DB_NAME]
    db[Config.COLLECTION_CAMPANAS_LOG].insert_one({
        "_id": f"{CAMPAIGN}:{CODE}", "campaign_id": CAMPAIGN, "property_code": CODE,
        "owner_email": EMAIL, "document_type": "COMMUNAL_MARKET_REPORT", "events": [],
    })
    portal_key = owner_property_portal_id(CODE, EMAIL)
    db["owner_property_portals"].insert_one({
        "_id": portal_key, "owner_key": portal_key, "property_code": CODE,
        "current_portal_state": {"period": "2026-10", "snapshot_hash": "snapshot-hash-10"},
    })
    monkeypatch.setattr(private_report, "MongoClient", lambda *_args, **_kwargs: mongo_client)
    monkeypatch.setattr(private_report, "GDriveSync", lambda: SimpleNamespace(service=None))
    monkeypatch.setattr(private_report, "_load_property_identity", lambda _code: None if document_type == "COMMUNAL_MARKET_REPORT" else {})
    expiry = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    report_token = issue_live_token(
        campaign_id=CAMPAIGN, property_code=CODE, action="ver_informe", recipient=EMAIL,
        document_type=document_type, expires_at=expiry, source="WHATSAPP",
        interaction_surface="OWNER_PORTAL",
    )
    session_id = str(uuid4())
    context_token = issue_live_token(
        campaign_id=CAMPAIGN, property_code=CODE, action="owner_portal_interaction", recipient=EMAIL,
        expires_at=expiry, source="WHATSAPP", interaction_surface="OWNER_PORTAL",
        report_period="2026-10", snapshot_hash="snapshot-hash-10", session_id=session_id,
    )
    response = private_report._serve_campaign_report(report_token, context_token, session_id)
    assert response.status_code in {200, 503}
    events = db[Config.COLLECTION_CAMPANAS_LOG].find_one({"_id": f"{CAMPAIGN}:{CODE}"})["events"]
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "report_opened" and event["action"] == "ver_informe"
    assert event["document_type"] == document_type
    assert event["report_period"] == "2026-10" and event["snapshot_hash"] == "snapshot-hash-10"
    assert event["interaction_channel"] == "WHATSAPP"
    assert event["session_id"] == session_id
    mongo_client.close()


@pytest.mark.parametrize("placement", ["TOP", "STICKY"])
@pytest.mark.parametrize("selected_type", ["RECOMMENDED", "GRADUAL"])
def test_review_and_authorization_events_preserve_placement_and_selection(monkeypatch, placement, selected_type):
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "telemetry-test-secret")
    db = make_db()
    claims = {
        "campaign_id": CAMPAIGN, "property_code": CODE, "recipient": EMAIL,
        "event_id": uuid4().hex, "source": "EMAIL", "interaction_surface": "OWNER_PORTAL",
        "cta_placement": placement, "report_period": "2026-10", "snapshot_hash": "snapshot-hash-10",
        "session_id": uuid4().hex,
    }
    persist_live_event(db, claims, event="cta_clicked", action="aceptar_rebaja")
    persist_live_event(db, claims, event="price_confirm_page_opened", action="aceptar_rebaja")
    details = {
        "selected_adjustment_type": selected_type, "selected_adjustment_pct": 9 if selected_type == "RECOMMENDED" else 5,
        "selected_price": 3119.025 if selected_type == "RECOMMENDED" else 3256.125,
        "selected_price_clp": 127400000 if selected_type == "RECOMMENDED" else 133000000,
    }
    persist_live_event(db, claims, event="price_authorized", action="aceptar_rebaja", details=details)
    events = db[Config.COLLECTION_CAMPANAS_LOG].find_one({"_id": f"{CAMPAIGN}:{CODE}"})["events"]
    auth = next(item for item in events if item["event"] == "price_authorized")
    selection = next(item for item in events if item["event"] in {"recommended_selected", "gradual_selected"})
    assert all(item["cta_placement"] == placement for item in events)
    assert selection["event"] == ("gradual_selected" if selected_type == "GRADUAL" else "recommended_selected")
    assert auth["selected_adjustment_type"] == selected_type
    assert auth["selected_adjustment_pct"] == details["selected_adjustment_pct"]
    assert auth["selected_price"] == details["selected_price"]
    assert auth["report_period"] == "2026-10" and auth["snapshot_hash"] == "snapshot-hash-10"
