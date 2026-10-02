from __future__ import annotations

import mongomock

from owner_portal.monthly import build_monthly_portal_view, owner_property_portal_id
from owner_portal.property_media import (
    parse_public_property_page,
    verified_historical_media,
    verified_media_from_property_record,
)


def listing_html(code="6811", image=None):
    image = image or f"https://demoazimg.prop360.cl/procasa/img/propiedades/{code}_main.JPEG"
    return f'''<html><head>
      <meta property="og:url" content="https://procasa.cl/{code}" />
      <meta property="og:image" content="{image}" />
    </head><body></body></html>'''


def test_public_listing_image_requires_exact_property_page_identity():
    result = parse_public_property_page(listing_html("6811"), property_code="6811")
    assert result["status"] == "FOUND"
    assert result["property_code_match"] is True
    assert result["hero_image_url"].endswith("6811_main.JPEG")

    mismatch = parse_public_property_page(listing_html("6812"), property_code="6811")
    assert mismatch["status"] == "IDENTITY_MISMATCH"
    assert mismatch["hero_image_url"] is None


def test_untrusted_image_host_is_not_used():
    result = parse_public_property_page(
        listing_html(image="https://unrelated.example/wrong.jpg"), property_code="6811"
    )
    assert result["property_code_match"] is True
    assert result["status"] == "IMAGE_MISSING"
    assert result["hero_image_url"] is None


def test_historical_image_fallback_requires_code_in_image_filename():
    assert verified_historical_media(
        '<img src="https://demoazimg.prop360.cl/procasa/img/propiedades/6811_main.JPEG">', "6811"
    )["image_source"] == "HISTORICAL_PROPERTY_IMAGE"
    assert verified_historical_media(
        '<img src="https://demoazimg.prop360.cl/procasa/img/propiedades/6812_main.JPEG">', "6811"
    ) is None


def test_existing_property_image_fallback_requires_matching_record_code():
    image = "https://demoazimg.prop360.cl/procasa/img/propiedades/6811_main.JPEG"
    assert verified_media_from_property_record(
        {"codigo": "6811", "main_image_url": image}, "6811"
    )["image_source"] == "EXISTING_PROPERTY_DATA"
    assert verified_media_from_property_record(
        {"codigo": "6812", "main_image_url": image}, "6811"
    ) is None


def test_monthly_portal_uses_only_verified_same_property_media_without_network():
    db = mongomock.MongoClient().test
    code = "6811"
    owner = "owner@example.test"
    key = owner_property_portal_id(code, owner)
    db["owner_property_portals"].insert_one({
        "_id": key,
        "owner_key": key,
        "property_code": code,
        "current_portal_state": {
            "period": "2026-10",
            "property_media": {
                "public_page_url": f"https://www.procasa.cl/{code}",
                "hero_image_url": f"https://demoazimg.prop360.cl/procasa/img/propiedades/{code}_main.JPEG",
                "image_source": "PROCASA_PUBLIC_PROPERTY",
                "verified_property_code": code,
                "public_page_active": True,
            },
        },
    })
    row = {"property_code": code, "owner_email": owner, "send_status": "SENT", "campaign_snapshot": {"owner_email": owner}}
    campaign_view = {"source": "EMAIL", "safe_mode": False, "document_available": False}
    view = build_monthly_portal_view(db, row, campaign_view)
    assert view["property_image_url"].endswith("6811_main.JPEG")

    db["owner_property_portals"].update_one(
        {"_id": key},
        {"$set": {"current_portal_state.property_media.verified_property_code": "6812"}},
    )
    rejected = build_monthly_portal_view(db, row, campaign_view)
    assert rejected["property_image_url"] is None


def test_monthly_media_preparation_is_idempotent_and_keeps_access_separate(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from scripts import refresh_owner_portal_property_media as refresh

    db = mongomock.MongoClient().test
    db["ajuste_precio"].insert_one({
        "campaign_id": "campaign",
        "property_code": "6811",
        "owner_email": "owner@example.test",
        "campaign_snapshot": {"source_live_sha": "abc"},
        "portal_access": {"status": "ACTIVE", "revoked_at": None, "expires_at": datetime.now(timezone.utc) + timedelta(days=30), "token_id": "keep-token"},
    })
    monkeypatch.setattr(refresh, "get_db", lambda: db)
    monkeypatch.setattr(refresh, "fetch_verified_property_media", lambda code: {
        "status": "FOUND", "property_code_match": True,
        "public_page_url": f"https://www.procasa.cl/{code}",
        "hero_image_url": f"https://demoazimg.prop360.cl/procasa/img/propiedades/{code}_main.JPEG",
        "image_source": "PROCASA_PUBLIC_PROPERTY", "verified_at": datetime.now(timezone.utc),
        "public_page_active": True, "image_render_pass": True,
    })

    dry = refresh.prepare(execute=False)
    assert dry["total"] == 1
    assert dry["found"] == 1
    assert db["owner_property_portals"].count_documents({}) == 0

    executed = refresh.prepare(execute=True)
    assert executed["found_persist_created"] == 1
    repeated = refresh.prepare(execute=True)
    assert repeated["snapshot_period_already_prepared"] == 1
    ledger_row = db["ajuste_precio"].find_one({"property_code": "6811"})
    assert ledger_row["portal_access"]["token_id"] == "keep-token"
    saved = db["owner_property_portals"].find_one({"property_code": "6811"})
    assert saved["current_portal_state"]["property_media"]["verified_property_code"] == "6811"
