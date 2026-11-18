"""Regression: unresolved genuine property links retry after initial inventory miss."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mongomock

from chatbot import inventory_reconciliation as recovery
from chatbot.commercial_intake import WAITING_INVENTORY


def _event(db, *, age_hours=1, due=None, external_id="MLC-123456789"):
    now = datetime.now(timezone.utc)
    doc = {
        "_id": f"case-{age_hours}-{external_id}",
        "phone": "+56912345678",
        "source_inbound_provider_id": f"provider-{age_hours}-{external_id}",
        "commercial_processing_state": WAITING_INVENTORY,
        "source_portal": "portal_inmobiliario",
        "source_external_id": external_id,
        "source_property_code": f"https://www.portalinmobiliario.com/{external_id}-departamento-en-arriendo-_JM",
        "received_at": now - timedelta(hours=age_hours),
        "created_at": now - timedelta(hours=age_hours),
    }
    if due is not None:
        doc["next_attempt_at"] = due
    db[recovery.COLLECTION].insert_one(doc)
    return doc


def test_no_inventory_delta_retries_due_pending_event(monkeypatch):
    db = mongomock.MongoClient().URLS
    event = _event(db)
    resumed = []
    monkeypatch.setattr(recovery, "resume_waiting_event", lambda db, event: (
        resumed.append(event["_id"]) or {"status": "completed"}
    ))
    report = recovery.reconcile_waiting_inventory_events(
        db, properties=[], limit=3, max_age_days=3,
    )
    assert report == {"candidates": 1, "completed": 1, "waiting": 0, "failed": 0, "skipped": 0}
    assert resumed == [event["_id"]]


def test_recent_worker_does_not_reopen_old_or_not_due_leads(monkeypatch):
    db = mongomock.MongoClient().URLS
    old = _event(db, age_hours=4 * 24, external_id="MLC-111111111")
    next_time = datetime.now(timezone.utc) + timedelta(hours=2)
    waiting = _event(db, age_hours=1, due=next_time, external_id="MLC-222222222")
    current = _event(db, age_hours=2, external_id="MLC-333333333")
    resumed = []
    monkeypatch.setattr(recovery, "resume_waiting_event", lambda db, event: (
        resumed.append(event["_id"]) or {"status": "completed"}
    ))
    report = recovery.reconcile_waiting_inventory_events(db, properties=None, limit=3, max_age_days=3)
    assert report["completed"] == 1
    assert resumed == [current["_id"]]
    assert old["_id"] not in resumed
    assert waiting["_id"] not in resumed


def test_changed_inventory_targets_only_matching_pending_reference(monkeypatch):
    db = mongomock.MongoClient().URLS
    matched = _event(db, external_id="MLC-123456789")
    _event(db, external_id="MLC-555555555")
    prop = {
        "codigo": "17081", "disponible_prop360": True,
        "publicaciones": {"portal_inmobiliario": {
            "publicaciones": {"A": {
                "code": "MLC123456789",
                "external_id": ["MLC-123456789"],
            }},
        }},
    }
    resumed = []
    monkeypatch.setattr(recovery, "resume_waiting_event", lambda db, event: (
        resumed.append(event["_id"]) or {"status": "completed"}
    ))
    report = recovery.reconcile_waiting_inventory_events(db, properties=[prop], limit=4)
    assert report["completed"] == 1
    assert resumed == [matched["_id"]]


def test_worker_runs_bounded_and_only_during_business_hours():
    source = (Path(__file__).resolve().parents[1] / "webhook.py").read_text(encoding="utf8")
    assert "next_property_reconcile_at = 0.0" in source
    assert 'os.getenv("PENDING_PROPERTY_AUTO_RECOVER_ENABLED", "true")' in source
    assert "next_property_reconcile_at = loop.time() + 300" in source
    assert "limit=3," in source
    assert "max_age_days=AUTO_RECONCILE_MAX_AGE_DAYS," in source
    assert "if should_send_now():" in source
