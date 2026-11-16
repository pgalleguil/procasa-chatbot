"""Post-scrape sending is opt-in, only for newly persisted source price drops."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest

from scripts import price_drop_after_scrape as hook
from scripts import enviar_rebajas_automatico as auto


RUN_ID = "manual_scrape_test_20261116"
NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)


def _db():
    return mongomock.MongoClient()["URLS"]


def _event(db, *, run_id=RUN_ID, code="17252", operation="arriendo",
           old=435000, new=400000, currency="CLP", typ="rebaja_confirmada"):
    db["universo_cartera_prop360_historial"].insert_one({
        "sync_run_id": run_id, "codigo": code, "campo": "precio_publicado",
        "tipo_evento": typ, "operacion": operation,
        "moneda_anterior": currency, "moneda_nueva": currency,
        "monto_anterior": old, "monto_nuevo": new,
        "fecha": NOW.isoformat(),
    })


def test_only_decreases_committed_by_this_scrape_can_trigger():
    db = _db()
    _event(db)
    _event(db, run_id="manual_scrape_older", code="6436", currency="UF", old=2790, new=2690)
    _event(db, code="90001", old=100, new=125)
    _event(db, code="90002", old=200, new=100, typ="unverified")
    keys = hook.verified_scrape_change_keys(db, sync_run_id=RUN_ID)
    assert keys == {("17252", "arriendo", "CLP", 435000.0, 400000.0)}
    assert hook.verified_scrape_change_keys(db, sync_run_id="wrong") == set()


def test_postscrape_is_disabled_without_opt_in_and_sends_nothing(monkeypatch):
    db = _db()
    _event(db)
    monkeypatch.delenv("PRICE_DROP_AFTER_SCRAPE_ENABLED", raising=False)
    assert hook.dispatch_after_scrape(sync_run_id=RUN_ID, started_at=NOW, db=db)["status"] == "DISABLED"
    assert db["leads"].count_documents({}) == 0


def test_outside_business_hours_or_missing_safety_gates_never_send(monkeypatch):
    db = _db()
    _event(db)
    monkeypatch.setenv("PRICE_DROP_AFTER_SCRAPE_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_LEADS_V2_SEND_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_LEADS_V2_CONTEXT_ENABLED", "true")
    assert hook.dispatch_after_scrape(sync_run_id=RUN_ID, started_at=NOW, db=db,
                                      now=NOW + timedelta(hours=12))["status"] == "BLOCKED_OUTSIDE_BUSINESS_HOURS"
    monkeypatch.delenv("PRICE_DROP_LEADS_V2_SEND_ENABLED")
    assert hook.dispatch_after_scrape(sync_run_id=RUN_ID, started_at=NOW, db=db,
                                      now=NOW)["status"] == "BLOCKED_CAMPAIGN_RUNTIME_GATES"


def test_postscrape_calls_existing_campaign_only_for_newly_verified_keys(monkeypatch):
    db = _db()
    _event(db)
    monkeypatch.setenv("PRICE_DROP_AFTER_SCRAPE_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_LEADS_V2_SEND_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_LEADS_V2_CONTEXT_ENABLED", "true")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    captured = []

    def fake_campaign_run(store, **kwargs):
        captured.append((store, kwargs))
        return {"status": "NO_ELIGIBLE_RECIPIENTS", "attempts": 0, "selected": 0}

    monkeypatch.setattr(auto, "run", fake_campaign_run)
    outcome = hook.dispatch_after_scrape(sync_run_id=RUN_ID, started_at=NOW, db=db, now=NOW)
    assert outcome["status"] == "NO_ELIGIBLE_RECIPIENTS"
    assert outcome["attempts"] == 0
    assert len(captured) == 1
    target, kwargs = captured[0]
    assert target is db
    assert kwargs["since"] == NOW
    assert kwargs["max_messages"] == 5
    assert kwargs["max_age_days"] == 90
    assert kwargs["min_delay"] == 300 and kwargs["max_delay"] == 600
    assert kwargs["allowed_change_keys"] == {("17252", "arriendo", "CLP", 435000.0, 400000.0)}


def test_no_new_price_drop_does_not_call_campaign(monkeypatch):
    db = _db()
    monkeypatch.setenv("PRICE_DROP_AFTER_SCRAPE_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_LEADS_V2_SEND_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_LEADS_V2_CONTEXT_ENABLED", "true")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)

    def never_called(*args, **kwargs):
        raise AssertionError("No campaign lookup or send expected")

    monkeypatch.setattr(auto, "run", never_called)
    result = hook.dispatch_after_scrape(sync_run_id=RUN_ID, started_at=NOW, db=db, now=NOW)
    assert result["status"] == "NO_NEW_VERIFIED_PRICE_DROPS"


def test_campaign_selection_excludes_unrelated_old_history(monkeypatch):
    db = _db()
    current = SimpleNamespace(property_code="17252", operation="Arriendo", currency="CLP",
                              old_price=435000, new_price=400000, dropped_at=NOW,
                              event_id="new")
    other = SimpleNamespace(property_code="6436", operation="Venta", currency="UF",
                            old_price=2790, new_price=2690, dropped_at=NOW,
                            event_id="old")
    calls = []

    def fake_events(_db, **kwargs):
        calls.append(kwargs)
        return [current, other]

    def fake_match(_db, event, **kwargs):
        calls.append(event.event_id)
        return []

    monkeypatch.setattr(auto.cli, "_events", fake_events)
    monkeypatch.setattr(auto, "find_candidates_for_event", fake_match)
    auto.choose_new_recipients(
        db, max_messages=5, max_age_days=90, now=NOW, since=NOW,
        allowed_change_keys={("17252", "arriendo", "CLP", 435000.0, 400000.0)},
    )
    assert calls[0]["since"] == NOW
    assert calls[1:] == ["new"]


def test_manual_scraper_post_hook_requires_successful_execute():
    script = (Path(__file__).resolve().parents[1] / "scraping_convecta"
              / "scraping_prop360_ficha_completa.py").read_text(encoding="utf-8")
    assert "if args.execute and outcome == 0:" in script
    assert "dispatch_after_scrape(" in script
    assert "sync_run_id=getattr(args, \"_manual_run_id\", None)" in script
    assert 'tipo_evento="rebaja_confirmada"' in script
