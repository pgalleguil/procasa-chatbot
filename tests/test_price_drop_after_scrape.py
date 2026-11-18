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


@pytest.mark.parametrize("before,after,verified,expected_decreases", [
    (435000, 400000, True, 1),
    (400000, 435000, True, 0),
    (400000, 400000, True, 0),
    (435000, 400000, False, 0),
])
def test_manual_upsert_creates_canonical_event_only_for_verified_real_reduction(
        monkeypatch, before, after, verified, expected_decreases):
    from copy import deepcopy
    from chatbot import uf_service
    from scraping_convecta import scraping_prop360_ficha_completa as scraper

    db = _db()
    old = {
        "codigo": "17252",
        "disponible_prop360": True,
        "estado": {"oficina": "PROCASA SUCRE"},
        "resumen": {"oficina": "PROCASA SUCRE"},
        "tipo_operacion": {
            "arriendo": True, "venta": False,
            "precio_arriendo": {
                "moneda_publicada": "CLP", "precio_publicado": before,
                "precio_publicado_original": before, "precio_clp": before,
                "moneda_precio_base": "CLP",
            },
        },
        "historial_cambios": [],
    }
    db["universo_cartera_prop360"].insert_one(deepcopy(old))
    new = deepcopy(old)
    price = new["tipo_operacion"]["precio_arriendo"]
    price.update({
        "precio_publicado": after, "precio_publicado_original": after,
        "precio_clp": after,
        "price_base_verified": verified,
        "fuente_moneda_publicacion": "prop360_editable_currency_radio",
    })
    monkeypatch.setattr(uf_service, "validar_uf_vigente", lambda info: (True, "verified"))
    monkeypatch.setattr(scraper, "canonicalize_prices", lambda doc, *a, **kw: doc)
    uf_info = {"valor": 40000, "fecha": "2026-11-16", "fuente": "test"}
    scraper.upsert_ficha(
        db["universo_cartera_prop360"], new, uf_info=uf_info,
        sync_run_id=RUN_ID, source="test_scrape",
    )
    decreases = list(db["universo_cartera_prop360_historial"].find({
        "sync_run_id": RUN_ID, "campo": "precio_publicado",
        "tipo_evento": "rebaja_confirmada",
    }))
    assert len(decreases) == expected_decreases
    if expected_decreases:
        row = decreases[0]
        assert row["operacion"] == "arriendo"
        assert row["moneda_anterior"] == row["moneda_nueva"] == "CLP"
        assert row["monto_anterior"] == 435000
        assert row["monto_nuevo"] == 400000
    assert hook.verified_scrape_change_keys(db, sync_run_id=RUN_ID) == (
        {("17252", "arriendo", "CLP", 435000.0, 400000.0)}
        if expected_decreases else set()
    )


def test_manual_scraper_loads_existing_external_env_before_config_import():
    src = (Path(__file__).resolve().parents[1] / "scraping_convecta"
           / "scraping_prop360_ficha_completa.py").read_text(encoding="utf-8")
    assert 'os.getenv("PRICE_DROP_ENV_FILE"' in src
    assert "load_dotenv(dotenv_path=_external_env, override=False)" in src
    assert src.index("load_dotenv(dotenv_path=_external_env") < src.index("from config import Config")


def test_post_scrape_business_hours_now_end_at_2000_chile():
    from zoneinfo import ZoneInfo
    from datetime import time as daytime
    tz = ZoneInfo("America/Santiago")
    local = datetime(2026, 11, 18, 19, 30, tzinfo=tz)
    assert hook._during_business_hours(local) is True
    assert hook._during_business_hours(local.replace(hour=19, minute=59)) is True
    assert hook._during_business_hours(local.replace(hour=20, minute=0)) is False


def test_auto_cli_end_time_is_propagated_without_sending(monkeypatch):
    from datetime import time as daytime
    from scripts import enviar_piloto_rebajas as cli
    from scripts import enviar_rebajas_automatico as auto
    monkeypatch.setattr(cli, "_get_db", lambda: object())
    calls = []
    def fake_run(db, **kwargs):
        calls.append(kwargs)
        return {"status": "NO_ELIGIBLE_RECIPIENTS", "attempts": 0}
    monkeypatch.setattr(auto, "run", fake_run)
    assert cli.main([
        "--auto", "--execute", "--max-messages", "5",
        "--start-time", "09:00", "--end-time", "20:00",
        "--lead-max-age-days", "90",
        "--min-delay-seconds", "300", "--max-delay-seconds", "600",
    ]) == 0
    assert calls[0]["start_time"] == daytime(9)
    assert calls[0]["end_time"] == daytime(20)
    assert calls[0]["timezone_name"] == "America/Santiago"
    assert calls[0]["max_messages"] == 5


def test_automatic_batch_forwards_window_to_real_sender(monkeypatch):
    from datetime import time as daytime
    from types import SimpleNamespace
    from scripts import enviar_rebajas_automatico as auto
    from chatbot import price_drop_leads_v2 as v2
    db = _db()
    now = datetime(2026, 11, 18, 21, 0, tzinfo=timezone.utc)
    event = SimpleNamespace(property_code="6436", operation="Venta", currency="UF",
                            current_price=2690, listing_url="https://www.procasa.cl/6436",
                            campaign_id="rebaja_6436_unit", event_id="rebaja_6436_unit")
    selected = [{"event": event, "candidate": {"eligible": True}, "lead_id": "507f1f77bcf86cd799439011", "last4": "0001"}]
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    monkeypatch.setattr(auto, "choose_new_recipients", lambda *args, **kwargs:
                        (selected, {"events_examined": 1, "events_discovered": 1}))
    monkeypatch.setattr(auto, "verify_publications", lambda *args, **kwargs:
                        {event.event_id: {"sendable": True, "status": "VERIFIED"}})
    monkeypatch.setattr(auto.cli, "_backup_leads", lambda *args, **kwargs: "/tmp/backup")
    monkeypatch.setattr(auto.cli, "prepare_selected", lambda *args, **kwargs: None)
    monkeypatch.setattr(auto.v2, "approve_one", lambda *args, **kwargs: None)
    kwargs_seen = []
    def fake_send(*args, **kwargs):
        kwargs_seen.append(kwargs)
        return {"status": "COMPLETED", "attempts": 1}
    monkeypatch.setattr(auto.cli, "execute_approved_batch", fake_send)
    result = auto.run(db, execute=True, max_messages=5, max_age_days=90,
                      min_delay=300, max_delay=600, now=now,
                      start_time=daytime(9), end_time=daytime(20))
    assert result["status"] == "COMPLETED"
    assert result["attempts"] == 1
    assert kwargs_seen[0]["end_time"] == daytime(20)
    assert kwargs_seen[0]["timezone_name"] == "America/Santiago"


def test_auto_selection_quarantines_extreme_currency_conversion_history(monkeypatch):
    """Do not advertise an implausible historical UF 1498288 -> UF 36.5."""
    from types import SimpleNamespace
    from scripts import enviar_rebajas_automatico as auto
    db = _db()
    strange = SimpleNamespace(property_code="16544", operation="Arriendo",
                              currency="UF", old_price=1498288, new_price=36.5,
                              event_id="suspicious", dropped_at=NOW)
    ordinary = SimpleNamespace(property_code="16972", operation="Arriendo",
                               currency="UF", old_price=63, new_price=60,
                               event_id="valid", dropped_at=NOW)
    monkeypatch.setattr(auto.cli, "_events", lambda *a, **kw: [strange, ordinary])
    reached = []
    def fake_find(*args, **kwargs):
        reached.append(args[1].event_id)
        return []
    monkeypatch.setattr(auto, "find_candidates_for_event", fake_find)
    recipients, report = auto.choose_new_recipients(
        db, max_messages=5, max_age_days=120, now=NOW,
    )
    assert recipients == []
    assert reached == ["valid"]
    assert report["events_needing_price_review"] == ["16544"]
    assert report["exclusions_by_reason"]["extreme_or_invalid_price_drop_event"] == 1


def test_auto_dry_run_explains_why_leads_were_excluded(monkeypatch):
    from types import SimpleNamespace
    from scripts import enviar_rebajas_automatico as auto
    db = _db()
    event = SimpleNamespace(property_code="16972", operation="Arriendo",
                            currency="UF", old_price=63, new_price=60,
                            event_id="valid", dropped_at=NOW)
    monkeypatch.setattr(auto.cli, "_events", lambda *a, **kw: [event])
    monkeypatch.setattr(auto, "find_candidates_for_event",
                        lambda *a, **kw: [
                            {"eligible": False, "reason": "lead_age_exceeded"},
                            {"eligible": False, "reason": "event_already_attempted"},
                            {"eligible": False, "reason": "lead_age_exceeded"},
                        ])
    report = auto.run(db, execute=False, max_messages=5, max_age_days=90,
                      now=NOW)
    assert report["status"] == "NO_ELIGIBLE_RECIPIENTS"
    assert report["selected"] == 0
    assert report["attempts"] == 0
    assert report["exclusions_by_reason"] == {
        "event_already_attempted": 1, "lead_age_exceeded": 2,
    }
    assert report["events_needing_price_review"] == []
