from __future__ import annotations

from datetime import datetime, timezone

import pytest
import mongomock

from campanas.owner_campaign_live_events import (
    _campaign_whatsapp_text,
)
from campanas.timezone_utils import DISPLAY_TIMEZONE, STORAGE_TIMEZONE, format_event_at_for_display
from scripts.cleanup_owner_portal_test_interactions import (
    AUTHORIZATION_FIELDS,
    BOOTSTRAP_LOCK_COLLECTION,
    BOOTSTRAP_LOCK_ID,
    BOOTSTRAP_NONCE,
    CleanupPreflightError,
    apply_preflight,
    _document_update,
    build_document_plan,
    build_preflight,
    email_integrity_snapshot,
    is_owner_portal_link_event,
    run_one_shot_cleanup,
)


def test_timestamp_display_uses_santiago_dst_rules_and_preserves_utc_storage() -> None:
    winter_utc = datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc)
    summer_utc = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)

    assert STORAGE_TIMEZONE is timezone.utc
    assert DISPLAY_TIMEZONE.key == "America/Santiago"
    assert format_event_at_for_display(winter_utc) == "16-07-2026 11:00 America/Santiago"
    assert format_event_at_for_display(summer_utc) == "06-10-2026 12:00 America/Santiago"
    assert format_event_at_for_display(datetime(2026, 10, 6, 15, 0)) == "06-10-2026 12:00 America/Santiago"


def test_admin_whatsapp_message_displays_chile_time_not_utc() -> None:
    text = _campaign_whatsapp_text(
        {"property_code": "5735", "owner_name": "Propietario", "campaign_id": "test"},
        "advisor_review_requested",
        datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert "Fecha: 06-10-2026 12:00 America/Santiago" in text
    assert "UTC" not in text


def _event(name: str, **fields: object) -> dict[str, object]:
    return {"event": name, **fields}


def test_event_removal_uses_surface_and_strict_legacy_source_rule() -> None:
    assert is_owner_portal_link_event(_event("cta_clicked", interaction_surface="OWNER_PORTAL"))
    assert is_owner_portal_link_event(_event("report_opened", source="EMAIL"))
    assert is_owner_portal_link_event(_event("executive_whatsapp_clicked", source="WHATSAPP"))
    assert not is_owner_portal_link_event(_event("price_authorized", interaction_surface="EMAIL_TEMPLATE", source="EMAIL"))
    assert not is_owner_portal_link_event(_event("cta_clicked"))
    assert not is_owner_portal_link_event(_event("cta_clicked", interaction_surface=None, source="EMAIL"))


def test_plan_preserves_email_and_sourceless_events_and_recalculates_derived_fields() -> None:
    old = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    recent = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    document = {
        "_id": "campaign:1",
        "campaign_id": "owner_price_sucre_wave1_20260928",
        "property_code": "9999",
        "events": [
            _event("cta_clicked", interaction_surface="OWNER_PORTAL", event_at=recent, event_id="portal"),
            _event("cta_clicked", event_at=old, event_id="legacy_sourceless"),
            _event("report_opened", interaction_surface="EMAIL_TEMPLATE", event_at=recent, event_id="email"),
            _event("executive_whatsapp_clicked", interaction_surface="OWNER_PORTAL", event_id="portal_wa"),
            _event("executive_whatsapp_clicked", interaction_surface="EMAIL_TEMPLATE", event_id="email_wa"),
        ],
        "owner_whatsapp_click_count": 2,
    }

    plan = build_document_plan(document)
    assert plan is not None
    assert [event["event_id"] for event in plan["events_remove"]] == ["portal", "portal_wa"]
    assert {event["event_id"] for event in plan["events_keep"]} == {"legacy_sourceless", "email", "email_wa"}
    _query, update = _document_update(plan)
    assert update["$set"]["last_cta_clicked_at"] == old
    assert update["$set"]["last_report_opened_at"] == recent
    assert update["$set"]["owner_whatsapp_click_count"] == 1
    assert "owner_whatsapp_click_count" not in update.get("$unset", {})
    assert not any(field in update.get("$unset", {}) for field in AUTHORIZATION_FIELDS)


def test_portal_authorization_reset_is_limited_to_explicit_target_code() -> None:
    document = {
        "_id": "campaign:5735",
        "campaign_id": "owner_price_sucre_wave1_20260928",
        "property_code": "5735",
        "events": [_event("price_authorized", interaction_surface="OWNER_PORTAL", event_id="portal_auth")],
        "authorization_status": "PRICE_AUTHORIZED",
        "price_authorized_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "selected_adjustment_type": "RECOMMENDED",
        "selected_adjustment_pct": 5,
        "selected_price": 100,
    }

    plan = build_document_plan(document)
    assert plan is not None and plan["reset_authorization"] is True
    _query, update = _document_update(plan)
    assert update["$set"]["authorization_status"] == "PENDING"
    assert set(update["$unset"]) >= set(AUTHORIZATION_FIELDS)


def test_portal_auth_reset_refuses_to_override_any_surviving_auth_event() -> None:
    document = {
        "_id": "campaign:5735",
        "campaign_id": "owner_price_sucre_wave1_20260928",
        "property_code": "5735",
        "events": [
            _event("price_authorized", interaction_surface="OWNER_PORTAL", event_id="portal_auth"),
            _event("price_authorized", event_id="legacy_unknown"),
        ],
        "authorization_status": "PRICE_AUTHORIZED",
    }

    with pytest.raises(CleanupPreflightError, match="surviving authorization evidence"):
        build_document_plan(document)


def test_notification_linked_to_owner_event_is_removed_but_legitimate_email_is_preserved() -> None:
    document = {
        "_id": "campaign:9999",
        "campaign_id": "owner_price_sucre_wave1_20260928",
        "property_code": "9999",
        "events": [
            _event("cta_clicked", interaction_surface="OWNER_PORTAL", event_id="portal_event"),
            _event("report_opened", interaction_surface="EMAIL_TEMPLATE", event_id="email_event"),
        ],
        "notifications": {"email": {
            "cta_clicked": {"event_id": "portal_event", "status": "sent"},
            "report_opened": {"event_id": "email_event", "status": "sent"},
        }},
    }

    plan = build_document_plan(document)
    assert plan is not None
    assert plan["notification_paths_to_remove"] == ["notifications.email.cta_clicked"]
    _query, update = _document_update(plan)
    assert update["$unset"]["notifications.email.cta_clicked"] == ""
    assert email_integrity_snapshot([document]) == email_integrity_snapshot([{
        **document,
        "events": [document["events"][1]],
        "notifications": {"email": {"report_opened": document["notifications"]["email"]["report_opened"]}},
    }])


def test_notification_with_event_id_shared_by_removed_and_email_event_aborts() -> None:
    document = {
        "_id": "campaign:9998",
        "campaign_id": "owner_price_sucre_wave1_20260928",
        "property_code": "9998",
        "events": [
            _event("cta_clicked", interaction_surface="OWNER_PORTAL", event_id="duplicate"),
            _event("report_opened", interaction_surface="EMAIL_TEMPLATE", event_id="duplicate"),
        ],
        "notifications": {"email": {"report_opened": {"event_id": "duplicate"}}},
    }
    with pytest.raises(CleanupPreflightError, match="shared by owner and preserved events"):
        build_document_plan(document)


def test_mocked_cleanup_preserves_email_data_and_only_removes_linked_admin_notification() -> None:
    client = mongomock.MongoClient()
    collection = client.URLS.ajuste_precio
    portal_auth_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    email_auth_at = datetime(2026, 9, 30, tzinfo=timezone.utc)
    portal_event = _event(
        "price_authorized", interaction_surface="OWNER_PORTAL", event_id="portal_auth",
        event_at=portal_auth_at,
    )
    email_event = _event(
        "price_authorized", interaction_surface="EMAIL_TEMPLATE", event_id="email_auth",
        event_at=email_auth_at, property_code="16999",
    )
    legacy_email_event = _event("report_opened", event_id="legacy_email")
    collection.insert_many([
        {
            "_id": "owner_price_sucre_wave1_20260928:5735",
            "campaign_id": "owner_price_sucre_wave1_20260928",
            "property_code": "5735",
            "events": [portal_event, legacy_email_event],
            "authorization_status": "PRICE_AUTHORIZED",
            "price_authorized_at": portal_auth_at,
            "selected_adjustment_type": "RECOMMENDED",
            "selected_adjustment_pct": 5,
            "selected_price": 100,
            "owner_whatsapp_click_count": 1,
            "notifications": {
                "email": {"price_authorized": {"event_id": "email_auth", "status": "sent"}},
                "admin_whatsapp": {"price_authorized": {"event_id": "portal_auth", "status": "sent"}},
            },
        },
        {
            "_id": "owner_price_sucre_wave1_20260928:16999",
            "campaign_id": "owner_price_sucre_wave1_20260928",
            "property_code": "16999",
            "events": [email_event],
            "authorization_status": "PRICE_AUTHORIZED",
            "price_authorized_at": email_auth_at,
            "selected_adjustment_type": "RECOMMENDED",
            "selected_adjustment_pct": 5,
            "selected_price": 200,
            "notifications": {"email": {"price_authorized": {"event_id": "email_auth", "status": "sent"}}},
        },
        {
            "_id": "owner_price_sucre_wave2_20260930:5641",
            "campaign_id": "owner_price_sucre_wave2_20260930",
            "property_code": "5641",
            "authorization_status": "PRICE_AUTHORIZED",
            "selected_price": 300,
        },
    ])
    before = list(collection.find({"campaign_id": {"$in": [
        "owner_price_sucre_wave1_20260928", "owner_price_sucre_wave2_20260930",
    ]}}))
    email_signature_before = email_integrity_snapshot(before)
    report = build_preflight(before)

    apply_preflight(collection, report)
    after = list(collection.find({"campaign_id": {"$in": [
        "owner_price_sucre_wave1_20260928", "owner_price_sucre_wave2_20260930",
    ]}}))
    by_code = {doc["property_code"]: doc for doc in after}

    owner = by_code["5735"]
    assert [event["event_id"] for event in owner["events"]] == ["legacy_email"]
    assert owner["authorization_status"] == "PENDING"
    assert "price_authorized_at" not in owner
    assert "selected_price" not in owner
    assert "owner_whatsapp_click_count" not in owner
    assert "price_authorized" not in owner["notifications"].get("admin_whatsapp", {})
    assert owner["notifications"]["email"]["price_authorized"]["event_id"] == "email_auth"

    email_owner = by_code["16999"]
    assert email_owner["authorization_status"] == "PRICE_AUTHORIZED"
    assert [event["event_id"] for event in email_owner["events"]] == ["email_auth"]
    assert email_owner["selected_price"] == 200

    residual = by_code["5641"]
    assert residual["authorization_status"] == "PENDING"
    assert "selected_price" not in residual
    assert email_integrity_snapshot(after) == email_signature_before


def test_cleanup_bootstrap_rejects_any_nonce_other_than_the_approved_one() -> None:
    assert run_one_shot_cleanup(mongo_uri="unused", nonce="unexpected", commit_sha="test") == {
        "status": "IGNORED_INVALID_NONCE"
    }


def test_cleanup_bootstrap_skips_when_atomic_marker_is_already_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = mongomock.MongoClient()
    client.URLS[BOOTSTRAP_LOCK_COLLECTION].insert_one({
        "_id": BOOTSTRAP_LOCK_ID,
        "status": "COMPLETED",
    })
    monkeypatch.setattr("scripts.cleanup_owner_portal_test_interactions.MongoClient", lambda *args, **kwargs: client)

    assert run_one_shot_cleanup(
        mongo_uri="mongodb://fake",
        nonce=BOOTSTRAP_NONCE,
        commit_sha="test-sha",
    ) == {"status": "ALREADY_COMPLETED"}


def test_cleanup_bootstrap_retries_a_failed_preflight_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    client = mongomock.MongoClient()
    locks = client.URLS[BOOTSTRAP_LOCK_COLLECTION]
    locks.insert_one({
        "_id": BOOTSTRAP_LOCK_ID,
        "status": "FAILED",
        "error_type": "CleanupPreflightError",
    })
    monkeypatch.setattr("scripts.cleanup_owner_portal_test_interactions.MongoClient", lambda *args, **kwargs: client)

    with pytest.raises(CleanupPreflightError, match="invariant mismatch"):
        run_one_shot_cleanup(
            mongo_uri="mongodb://fake",
            nonce=BOOTSTRAP_NONCE,
            commit_sha="test-sha",
        )
    marker = locks.find_one({"_id": BOOTSTRAP_LOCK_ID})
    assert marker["status"] == "FAILED"
    assert marker["retry_count"] == 1

    assert run_one_shot_cleanup(
        mongo_uri="mongodb://fake",
        nonce=BOOTSTRAP_NONCE,
        commit_sha="test-sha",
    ) == {"status": "ALREADY_FAILED"}
