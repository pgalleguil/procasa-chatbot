from __future__ import annotations

from datetime import datetime, timedelta, timezone

import mongomock

from campanas import handler
from campanas.owner_campaign_live_events import (
    decode_live_token,
    derive_interaction_attribution,
    issue_attributed_followup_token,
    issue_live_token,
    persist_live_event,
)
from config import Config


WAVE1 = "owner_price_sucre_wave1_20260928"
WAVE2 = "owner_price_sucre_wave2_20260930"
CODE = "16469"
OWNER = "owner@example.com"


def claims(campaign_id=WAVE1, **overrides):
    value = {
        "campaign_id": campaign_id,
        "property_code": CODE,
        "recipient": OWNER,
        "exp": int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp()),
        "event_id": "attribution-test-event",
        "test_mode": False,
    }
    value.update(overrides)
    return value


def make_db(campaign_id=WAVE1, *, send_status="SENT"):
    db = mongomock.MongoClient()["test"]
    db[Config.COLLECTION_CAMPANAS_LOG].insert_one({
        "_id": f"{campaign_id}:{CODE}",
        "campaign_id": campaign_id,
        "property_code": CODE,
        "owner_email": OWNER,
        "send_status": send_status,
        "authorization_status": "PENDING",
        "events": [],
    })
    return db


def test_historical_wave_email_tokens_without_source_are_inferred_without_token_changes(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "local-attribution-secret")
    for campaign_id in (WAVE1, WAVE2):
        token = issue_live_token(
            campaign_id=campaign_id,
            property_code=CODE,
            action="aceptar_rebaja",
            recipient=OWNER,
            expires_at=int((datetime.now(timezone.utc) + timedelta(days=10)).timestamp()),
        )
        token_claims = decode_live_token(token)
        assert token_claims is not None
        assert "source" not in token_claims
        assert derive_interaction_attribution(token_claims) == {
            "interaction_surface": "EMAIL_TEMPLATE",
            "interaction_channel": "EMAIL",
        }


def test_unknown_source_less_legacy_flow_stays_unknown():
    assert derive_interaction_attribution(claims("legacy_campaign")) == {
        "interaction_surface": "UNKNOWN",
        "interaction_channel": "UNKNOWN",
    }


def test_portal_source_dimensions_are_distinct_for_whatsapp_and_email():
    assert derive_interaction_attribution(claims(source="WHATSAPP", interaction_surface="OWNER_PORTAL")) == {
        "interaction_surface": "OWNER_PORTAL",
        "interaction_channel": "WHATSAPP",
    }
    assert derive_interaction_attribution(claims(source="EMAIL", interaction_surface="OWNER_PORTAL")) == {
        "interaction_surface": "OWNER_PORTAL",
        "interaction_channel": "EMAIL",
    }


def test_event_types_persist_origin_attribution(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "local-attribution-secret")
    for event, action, event_claims, details in (
        ("price_authorized", "aceptar_rebaja", claims(source="WHATSAPP", interaction_surface="OWNER_PORTAL"), {
            "selected_adjustment_type": "RECOMMENDED",
            "selected_adjustment_pct": 7,
            "selected_price": 3000,
        }),
        ("report_opened", "ver_informe", claims(source="WHATSAPP", interaction_surface="OWNER_PORTAL"), {}),
        ("advisor_review_requested", "contactar_ejecutivo", claims(source="WHATSAPP", interaction_surface="OWNER_PORTAL"), {}),
        ("cta_clicked", "aceptar_rebaja", claims(), {}),
        ("confirmation_page_opened", "aceptar_rebaja", claims(), {}),
        ("price_confirm_page_opened", "aceptar_rebaja", claims(), {}),
    ):
        db = make_db(event_claims["campaign_id"])
        _, inserted = persist_live_event(db, event_claims, event=event, action=action, details=details)
        assert inserted
        stored = db[Config.COLLECTION_CAMPANAS_LOG].find_one({"_id": f"{event_claims['campaign_id']}:{CODE}"})
        persisted = next(item for item in stored["events"] if item["event"] == event)
        expected = derive_interaction_attribution(event_claims)
        assert persisted["interaction_surface"] == expected["interaction_surface"]
        assert persisted["interaction_channel"] == expected["interaction_channel"]
        if event_claims.get("source"):
            assert persisted["source"] == event_claims["source"]


def test_report_and_advisor_events_from_historical_email_are_attributed_to_email():
    for event, action in (("report_opened", "ver_informe"), ("advisor_review_requested", "contactar_ejecutivo")):
        db = make_db(WAVE2)
        persist_live_event(db, claims(WAVE2), event=event, action=action)
        stored = db[Config.COLLECTION_CAMPANAS_LOG].find_one({"_id": f"{WAVE2}:{CODE}"})
        persisted = next(item for item in stored["events"] if item["event"] == event)
        assert persisted["interaction_surface"] == "EMAIL_TEMPLATE"
        assert persisted["interaction_channel"] == "EMAIL"


def test_followup_advisor_token_preserves_portal_source_and_surface(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "local-attribution-secret")
    original = claims(source="WHATSAPP", interaction_surface="OWNER_PORTAL")
    token = issue_attributed_followup_token(original, action="contactar_ejecutivo")
    decoded = decode_live_token(token)
    assert decoded is not None
    assert decoded["action"] == "contactar_ejecutivo"
    assert decoded["source"] == "WHATSAPP"
    assert decoded["interaction_surface"] == "OWNER_PORTAL"


def test_stale_row_rejects_server_side_price_authorization(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "local-attribution-secret")
    db = make_db(send_status="SKIPPED_STALE_OR_MISMATCH")

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def __getitem__(self, _name):
            return db

        def close(self):
            pass

    monkeypatch.setattr("pymongo.MongoClient", Client)
    token = issue_live_token(
        campaign_id=WAVE1,
        property_code=CODE,
        action="aceptar_rebaja",
        recipient=OWNER,
        expires_at=int((datetime.now(timezone.utc) + timedelta(days=10)).timestamp()),
        source="WHATSAPP",
        interaction_surface="OWNER_PORTAL",
    )

    response = handler._process_owner_campaign_action(
        email=OWNER,
        accion="aceptar_rebaja",
        codigo=CODE,
        campana=WAVE1,
        token=token,
        method="POST",
        selected_adjustment_type="RECOMMENDED",
    )

    assert response.status_code == 409
    stored = db[Config.COLLECTION_CAMPANAS_LOG].find_one({"_id": f"{WAVE1}:{CODE}"})
    assert stored["authorization_status"] == "PENDING"
    assert stored["events"] == []
