from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from bson import ObjectId

from chatbot import price_drop_leads_v2 as v2
from chatbot.price_drop_campaign_classifier import classify_price_drop_reply
from chatbot.price_drop_campaign_discovery import (
    discover_price_drops, find_candidates_for_event,
)
from chatbot.price_drop_publication_check import (
    PublicationCheck, extract_public_prices, verify_current_listing,
)
from chatbot.wasender_usage import (
    WaSenderUsageUnavailable, account_attempts_last_24h,
)
from chatbot.price_drop_reconciliation import (
    pending_receipts, record_accepted_receipt, reconcile_pending_receipts,
)
from scripts import enviar_piloto_rebajas as cli

NOW = datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)  # Monday, 10:00 in Santiago.
DROP = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
CAMPAIGN = "event_test_6436_20261014"


def _db():
    return mongomock.MongoClient()["URLS"]


def _seed_property(db, *, code="6436", currency="UF", current=2690,
                   available=True, published=True, aliases=None):
    pricing = {"moneda_publicada": currency, "precio_publicado": current}
    if currency == "UF":
        pricing["precio_uf"] = current
    db["universo_cartera_prop360"].insert_one({
        "codigo": code, "disponible_prop360": available,
        "estado": {"oficina": "PROCASA SUCRE"},
        "ubicacion": {"comuna": "San Miguel"},
        "tipo_operacion": {"venta": True, "tipo": "Departamento", "precio_venta": pricing},
        "publicaciones": {
            "aliases": aliases or [],
            "procasa": {"publicaciones": {"venta": {
                "publicada": published, "estado": "ACTIVA", "operacion": "venta",
                "url": f"https://www.procasa.cl/propiedad/{code}",
            }}},
        },
    })


def _history(db, code, at, old, new, currency="UF", *, history_id=None):
    db["universo_cartera_prop360_historial"].insert_one({
        "_id": history_id or ObjectId(), "codigo": code,
        "campo": "precio_publicado", "fecha": at.isoformat(),
        "moneda_anterior": currency, "moneda_nueva": currency,
        "monto_anterior": old, "monto_nuevo": new,
    })


def _event_db(*, code="6436", currency="UF", current=2690,
              old=2790, new=2690, at=DROP, aliases=None):
    db = _db()
    _seed_property(db, code=code, currency=currency, current=current, aliases=aliases)
    _history(db, code, at, old, new, currency)
    return db


def _candidate(db, *, phone, temperature="NORMAL", content=None, timestamp=None,
               lead_id=None, campaigns=None, extra=None):
    lead_id = lead_id or ObjectId()
    row = {
        "_id": lead_id, "phone": phone,
        "contact_phone_normalized": phone,
        "lead_temperature_effective": temperature,
        "created_at": NOW - timedelta(days=60),
        "pipeline_stage": "CONTACTED", "stage": "CONTACTED",
        "messages": [{
            "role": "user", "content": content or "Me interesa la propiedad código 6436",
            "timestamp": (timestamp or DROP - timedelta(days=1)).isoformat(),
        }],
    }
    if campaigns is not None:
        row["price_drop_v2"] = {"campaigns": campaigns}
    if extra:
        row.update(extra)
    db["leads"].insert_one(row)
    return lead_id


def _dispatch_db(monkeypatch, count=3):
    db = _event_db()
    event = discover_price_drops(db, now=NOW)[0]
    ids = []
    phones = ["+56983219801", "+56983219802", "+56983219803", "+56983219805"]
    temperatures = ["HOT", "NORMAL", "COLD", "HOT"]
    for index in range(count):
        lead_id = _candidate(
            db, phone=(phones[index] if index < len(phones)
                       else f"+5691234{index:04d}"),
            temperature=temperatures[index % len(temperatures)],
            timestamp=DROP - timedelta(days=10),
        )
        phone = db["leads"].find_one({"_id": lead_id})["phone"]
        db["leads"].update_one({"_id": lead_id}, {"$push": {"messages": {
            "role": "user", "content": "Sigo interesado en la propiedad código 6436",
            "timestamp": (NOW - timedelta(hours=1)).isoformat(),
        }}})
        ids.append(str(lead_id))
        v2.prepare_one(db, now=NOW, lead_id=lead_id, phone=phone,
                       campaign_id=CAMPAIGN, property_code="6436",
                       old_price_uf=2790, new_price_uf=2690,
                       test_mode=False, contact_basis_reviewed=True,
                       contact_basis_type=v2.DIRECT_RESPONSE_BASIS_TYPE,
                       contact_basis="Consulta verificable de la propiedad")
        v2.review_whatsapp_contact(
            db, lead_id=lead_id, campaign_id=CAMPAIGN,
            operator="QA operator",
            evidence="Opt-in WhatsApp CRM reference 2026-10-01", now=NOW,
        )
        v2.approve_one(db, lead_id=lead_id, campaign_id=CAMPAIGN,
                       operator="QA operator", now=NOW)
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, CAMPAIGN)
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.WHATSAPP_CONTACT_REVIEWED_FLAG, "true")
    monkeypatch.setenv(v2.REAL_CAMPAIGNS_ENABLED_FLAG, "true")
    monkeypatch.delenv("PRICE_DROP_WHATSAPP_CHANNEL_MODE", raising=False)
    return db, event, ids


def _run(db, *, campaign_id=CAMPAIGN, max_messages=20, daily_cap=50,
         confirm=None, sleep_fn=None, sender=None, now_fn=lambda: NOW,
         link_verifier=lambda code, now: True, journal_path=None, progress_path=None,
         progress_fn=None, rng=lambda lo, hi: 450):
    return cli.execute_campaign(
        db, campaign_id=campaign_id, max_messages=max_messages,
        min_delay_seconds=300, max_delay_seconds=600,
        provider_plan="trial", daily_provider_cap=daily_cap,
        live_links_verified=True, confirm=confirm or (lambda _: f"ENVIAR {campaign_id}"),
        sleep_fn=sleep_fn or (lambda seconds: None),
        sender=sender or (lambda phone, text: {
            "success": True, "provider_message_id": f"mock-{phone[-4:]}",
            "delivery_status": "accepted",
        }), now_fn=now_fn, provider_ready=lambda: True,
        usage_reader=lambda stamp: 0,
        journal_path=journal_path, rng=rng, link_verifier=link_verifier,
        progress_path=progress_path, progress_fn=progress_fn,
    )


def test_discovery_finds_only_current_step_and_preserves_multistep_history():
    db = _db()
    _seed_property(db, current=2490)
    _history(db, "6436", DROP - timedelta(days=2), 2790, 2690)
    _history(db, "6436", DROP - timedelta(days=1), 2690, 2590)
    _history(db, "6436", DROP, 2590, 2490, history_id="latest-step")
    events = discover_price_drops(db, now=NOW)
    assert len(events) == 1
    assert (events[0].old_price, events[0].new_price, events[0].current_price) == (2590, 2490, 2490)
    assert events[0].history_id == "latest-step"
    assert len(cli._history_steps(db, events[0])) == 3
    after_notice = cli._history_steps(db, events[0], DROP - timedelta(days=1, seconds=1))
    assert [(row.old_price, row.new_price) for row in after_notice] == [(2690, 2590), (2590, 2490)]


def test_discovery_discards_increases_currency_conversions_and_stale_prices():
    db = _event_db(current=2690)
    _history(db, "6436", DROP + timedelta(hours=1), 2690, 2790)  # Increase.
    db["universo_cartera_prop360_historial"].insert_one({
        "codigo": "6436", "campo": "precio_publicado", "fecha": DROP.isoformat(),
        "moneda_anterior": "UF", "moneda_nueva": "CLP",
        "monto_anterior": 2690, "monto_nuevo": 100000000,
    })
    _history(db, "6436", DROP, 2790, 2690, history_id="duplicate-event-row")
    events = discover_price_drops(db, now=NOW)
    assert len(events) == 1
    assert events[0].currency == "UF"
    assert events[0].new_price == 2690


@pytest.mark.parametrize("available,published,current", [
    (False, True, 2690), (True, False, 2690), (True, True, 2675),
])
def test_discovery_rejects_unavailable_unpublished_or_changed_property(available, published, current):
    db = _db()
    _seed_property(db, available=available, published=published, current=current)
    _history(db, "6436", DROP, 2790, 2690)
    assert discover_price_drops(db, now=NOW) == []


def test_discovery_keeps_clp_without_converting_and_stable_event_identity():
    db = _event_db(code="16479", currency="CLP", current=152000000,
                   old=160000000, new=152000000)
    event = discover_price_drops(db, now=NOW)[0]
    assert event.currency == "CLP"
    assert event.old_price == 160000000 and event.new_price == 152000000
    assert event.event_id == discover_price_drops(db, now=NOW)[0].event_id


def test_discovery_full_scan_finds_rebate_after_more_than_five_thousand_history_rows():
    db = _event_db()
    rows = [{
        "_id": ObjectId(), "codigo": "6436", "campo": "precio_publicado",
        "fecha": (NOW + timedelta(seconds=index + 1)).isoformat(),
        "moneda_anterior": "UF", "moneda_nueva": "UF",
        "monto_anterior": 2690, "monto_nuevo": 2700,
    } for index in range(5001)]
    db["universo_cartera_prop360_historial"].insert_many(rows)
    assert discover_price_drops(db, now=NOW, limit=5000, batch_size=500) == []
    full = discover_price_drops(db, now=NOW, limit=None, batch_size=500)
    assert len(full) == 1 and full[0].new_price == 2690


def test_discovery_filters_since_property_operation_and_currency():
    db = _event_db()
    event = discover_price_drops(db, now=NOW, since=DROP - timedelta(hours=1),
                                 property_code="6436", operation="Venta",
                                 currency="UF", batch_size=1)
    assert len(event) == 1
    assert discover_price_drops(db, now=NOW, since=DROP + timedelta(days=1)) == []
    assert discover_price_drops(db, now=NOW, property_code="9999") == []
    assert discover_price_drops(db, now=NOW, currency="CLP") == []


def test_lead_who_asked_between_drops_is_eligible_for_the_later_drop_only():
    db = _event_db()
    event1 = discover_price_drops(db, now=NOW)[0]
    event2 = replace(event1, event_id="rebaja_6436_20261015T150000_UF_later",
                     campaign_id="rebaja_6436_20261015T150000_UF_later",
                     dropped_at=DROP + timedelta(days=1), old_price=2690,
                     new_price=2500, current_price=2500)
    lead_id = _candidate(
        db, phone="+56983219801", timestamp=DROP + timedelta(hours=1),
        content="Quiero saber más de la propiedad código 6436",
    )
    old_result = find_candidates_for_event(db, event1)[0]
    later_result = find_candidates_for_event(db, event2)[0]
    assert str(lead_id) == old_result["lead_id"] == later_result["lead_id"]
    assert old_result["reason"] == "no_dated_property_inquiry_before_drop"
    assert later_result["eligible"] is True


def test_yapo_historical_alias_is_a_property_specific_inquiry():
    db = _event_db(code="16479", currency="CLP", current=152000000,
                   old=160000000, new=152000000,
                   aliases=[{"external_id": "32248293", "resolvable": True}])
    event = discover_price_drops(db, now=NOW)[0]
    lead_id = _candidate(db, phone="+56983219801",
                         content="Estoy interesado en https://www.yapo.cl/propiedades/32248293")
    row = find_candidates_for_event(db, event)[0]
    assert row["lead_id"] == str(lead_id)
    assert row["eligible"] is True
    assert row["inquiry_reference"] == "listing_id:32248293"


def test_candidate_selection_prioritizes_all_temperatures_and_verifies_alias_dates():
    db = _event_db(aliases=[{"external_id": "MLC-3776431868", "resolvable": True}])
    event = discover_price_drops(db, now=NOW)[0]
    ids = [
        _candidate(db, phone="+56983219801", temperature="COLD",
                   content="https://departamento.mercadolibre.cl/MLC-3776431868"),
        _candidate(db, phone="+56983219802", temperature="NORMAL"),
        _candidate(db, phone="+56983219803", temperature="HOT"),
        _candidate(db, phone="+56983219805", temperature="HOT",
                   timestamp=DROP + timedelta(minutes=1)),
    ]
    rows = find_candidates_for_event(db, event)
    by_id = {row["lead_id"]: row for row in rows}
    assert by_id[str(ids[0])]["eligible"] is True
    assert by_id[str(ids[0])]["inquiry_reference"].startswith("listing_id:")
    assert by_id[str(ids[1])]["eligible"] is True
    assert by_id[str(ids[2])]["eligible"] is True
    assert by_id[str(ids[3])]["reason"] == "no_dated_property_inquiry_before_drop"
    assert [row["temperature"] for row in rows[:3]] == ["HOT", "NORMAL", "COLD"]


def test_unknown_temperature_is_pending_review_but_does_not_discard_verified_inquiry():
    db = _event_db()
    event = discover_price_drops(db, now=NOW)[0]
    lead_id = _candidate(db, phone="+56983219801", temperature="",
                         extra={"lead_temperature_effective": "SIN_CLASIFICAR"})
    row = find_candidates_for_event(db, event)[0]
    assert row["lead_id"] == str(lead_id)
    assert row["eligible"] is False
    assert row["temperature"] == "REVISIÓN_PENDIENTE"
    assert row["reason"] == "temperature_review_pending"


@pytest.mark.parametrize("temperature,selected,eligible", [
    ("HOT", "HOT", True), ("NORMAL", "HOT,NORMAL", True),
    ("COLD", "HOT,NORMAL", False), ("COLD", "COLD", True),
    ("SIN_CLASIFICAR", "ALL", False),
])
def test_temperature_filters_and_manual_unknown_review(temperature, selected, eligible):
    db = _event_db()
    event = discover_price_drops(db, now=NOW)[0]
    lead_id = _candidate(db, phone="+56983219801", temperature=temperature)
    row = find_candidates_for_event(
        db, event, temperature_filter=selected, lead_max_age_days=120, now=NOW,
    )[0]
    assert row["lead_id"] == str(lead_id)
    assert row["eligible"] is eligible
    if temperature == "SIN_CLASIFICAR":
        reviewed = find_candidates_for_event(
            db, event, temperature_filter="ALL", lead_max_age_days=120,
            temperature_reviews={str(lead_id): {
                "reviewed_temperature": "NORMAL", "evidence": "CRM revisión 2026-11",
                "reviewed_by": "Operador QA",
            }}, now=NOW,
        )[0]
        assert reviewed["eligible"] is True
        assert reviewed["effective_temperature"] == "NORMAL"


@pytest.mark.parametrize("days,created_type,expected", [
    (30, "datetime", True), (90, "iso_offset", True),
    (120, "iso_local", True), (180, "datetime", False),
])
def test_lead_age_filter_uses_created_at_and_chile_calendar(days, created_type, expected):
    db = _event_db()
    event = discover_price_drops(db, now=NOW)[0]
    created = NOW - timedelta(days=days)
    if created_type == "iso_offset":
        created_value = created.isoformat()
    elif created_type == "iso_local":
        created_value = created.astimezone(cli.CHILE_TZ).replace(tzinfo=None).isoformat()
    else:
        created_value = created.replace(tzinfo=None)
    lead_id = _candidate(db, phone="+56983219801", extra={"created_at": created_value})
    row = find_candidates_for_event(
        db, event, temperature_filter="HOT,NORMAL", lead_max_age_days=120, now=NOW,
    )[0]
    assert row["lead_id"] == str(lead_id)
    assert row["eligible"] is expected
    assert row["excluded_by_age"] is (not expected)


@pytest.mark.parametrize("created_at,reason", [
    (None, "created_at_missing_review_pending"),
    ("not-a-date", "created_at_invalid_review_pending"),
])
def test_missing_or_invalid_created_at_is_review_pending(created_at, reason):
    db = _event_db()
    event = discover_price_drops(db, now=NOW)[0]
    lead_id = _candidate(db, phone="+56983219801", extra={"created_at": created_at})
    row = find_candidates_for_event(db, event, now=NOW)[0]
    assert row["lead_id"] == str(lead_id)
    assert not row["eligible"]
    assert row["reason"] == reason
    assert row["age_review_required"] is True


def test_message_personalization_and_deterministic_variants_include_verified_facts():
    db = _event_db()
    event = discover_price_drops(db, now=NOW)[0]
    lead_id = _candidate(db, phone="+56983219801", extra={
        "prospecto": {"nombre": "Carolina Pérez"},
    })
    candidate = find_candidates_for_event(db, event, now=NOW)[0]
    assert candidate["lead_id"] == str(lead_id)
    assert candidate["first_name"] == "Carolina"
    variant = cli.message_variant_id(str(lead_id), event.event_id)
    message = cli.event_message(event, recipient_name=candidate["first_name"], variant_id=variant)
    assert "Hola, Carolina." in message
    assert "PROCASA" in message and "6436" in message
    assert "UF 2.790" in message and "UF 2.690" in message
    assert "UF 100" in message and event.listing_url in message
    assert "no recibir más avisos" in message
    assert variant == cli.message_variant_id(str(lead_id), event.event_id)
    generic = cli.event_message(event, recipient_name=None, variant_id="rebaja_v2")
    assert "Hola, ¿cómo estás?" in generic
    assert "Hola, Ejecutivo." not in generic
    db["leads"].update_one({"_id": lead_id}, {"$set": {
        "prospecto.nombre": "Ejecutivo Comercial", "nombre": None,
        "propietario.nombre": "Alberto Propietario",
        "captador.nombre": "María Captadora",
    }})
    invalid_name_row = find_candidates_for_event(db, event, now=NOW)[0]
    assert invalid_name_row["first_name"] is None


def test_filters_are_rechecked_before_send_after_campaign_approval(monkeypatch):
    db = _event_db()
    event = discover_price_drops(db, now=NOW)[0]
    lead_id = _candidate(db, phone="+56983219801", temperature="HOT")
    cli.prepare_selected(
        db, event=event, lead_ids=[str(lead_id)], operator="QA",
        contact_basis_reviewed=True, whatsapp_contact_reviewed=True,
        whatsapp_contact_evidence="CRM opt-in reference 2026-11",
        temperature_filter="HOT", lead_max_age_days=120, now=NOW,
    )
    v2.approve_one(db, lead_id=lead_id, campaign_id=event.event_id,
                   operator="QA", now=NOW)
    monkeypatch.setenv(v2.SEND_FLAG, "true")
    monkeypatch.setenv(v2.APPROVED_ID_FLAG, event.event_id)
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    monkeypatch.setenv(v2.REAL_CAMPAIGNS_ENABLED_FLAG, "true")
    monkeypatch.setenv(v2.WHATSAPP_CONTACT_REVIEWED_FLAG, "true")
    monkeypatch.setenv("PRICE_DROP_WHATSAPP_CHANNEL_MODE", "wasenderapi")
    calls = []
    db["leads"].update_one({"_id": lead_id}, {"$set": {"lead_temperature_effective": "COLD"}})
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id=event.event_id,
        temperature_filter_override="HOT", lead_max_age_days_override=120,
        enforce_campaign_filters=True,
        sender=lambda *args: calls.append(args), now=NOW,
    )
    assert result["status"] == "BLOCKED_TEMPERATURE_FILTER_MISMATCH"
    assert calls == []

    db["leads"].update_one({"_id": lead_id}, {"$set": {
        "lead_temperature_effective": "HOT",
        "created_at": NOW - timedelta(days=180),
    }})
    result = v2.send_one(
        db, lead_id=lead_id, campaign_id=event.event_id,
        temperature_filter_override="HOT", lead_max_age_days_override=120,
        enforce_campaign_filters=True,
        sender=lambda *args: calls.append(args), now=NOW,
    )
    assert result["status"] == "BLOCKED_LEAD_AGE_EXCEEDED"
    assert calls == []


def test_candidate_selection_rejects_duplicate_identity_opt_out_and_completed_search():
    db = _event_db()
    event = discover_price_drops(db, now=NOW)[0]
    duplicate_id = _candidate(db, phone="+56983219801")
    db["leads"].insert_one({"phone": "56983219801", "messages": []})
    opted_out_id = _candidate(db, phone="+56983219802", extra={
        "price_drop_v2": {"opted_out": True},
    })
    closed_id = _candidate(db, phone="+56983219803", extra={"pipeline_stage": "CLOSED_WON"})
    rows = {row["lead_id"]: row for row in find_candidates_for_event(db, event)}
    assert rows[str(duplicate_id)]["reason"] == "duplicate_phone"
    assert rows[str(opted_out_id)]["eligible"] is False
    assert rows[str(closed_id)]["reason"] == "closed_opportunity"


def test_dedupe_blocks_same_event_unknown_and_declined_but_allows_new_drop():
    db = _event_db()
    event = discover_price_drops(db, now=NOW)[0]
    sent_id = _candidate(db, phone="+56983219801", campaigns=[{
        "campaign_id": event.event_id, "property_code": "6436", "status": "SENT",
        "sent_at": DROP + timedelta(days=1), "new_price_uf": 2690,
    }])
    unresolved_id = _candidate(db, phone="+56983219802", campaigns=[{
        "campaign_id": "older-event", "property_code": "6436", "status": "DELIVERY_UNKNOWN",
    }])
    declined_id = _candidate(db, phone="+56983219803", campaigns=[{
        "campaign_id": "old-decline", "property_code": "6436", "status": "DECLINED",
    }])
    new_event = replace(
        event, event_id="rebaja_6436_20261101T120000_UF_new", campaign_id="rebaja_6436_20261101T120000_UF_new",
        dropped_at=DROP + timedelta(days=18), old_price=2690, new_price=2590,
        current_price=2590,
    )
    result = {row["lead_id"]: row for row in find_candidates_for_event(db, event)}
    assert result[str(sent_id)]["reason"] == "event_already_attempted"
    assert result[str(unresolved_id)]["reason"] == "unresolved_previous_delivery"
    assert result[str(declined_id)]["reason"] == "property_declined"
    new_result = {row["lead_id"]: row for row in find_candidates_for_event(db, new_event)}
    assert new_result[str(sent_id)]["eligible"] is True


def test_prepare_new_event_stores_cumulative_history_and_reference_price(monkeypatch):
    db = _db()
    _seed_property(db, current=2490)
    _history(db, "6436", DROP - timedelta(days=2), 2790, 2690)
    _history(db, "6436", DROP - timedelta(days=1), 2690, 2590)
    _history(db, "6436", DROP, 2590, 2490)
    event = discover_price_drops(db, now=NOW)[0]
    lead_id = _candidate(db, phone="+56983219801", temperature="HOT",
                         timestamp=DROP - timedelta(days=10))
    db["leads"].update_one({"_id": lead_id}, {"$push": {
        "price_drop_v2.campaigns": {
            "campaign_id": "previous-price-notice", "property_code": "6436",
            "status": "SENT", "sent_at": DROP - timedelta(days=2) + timedelta(hours=1),
            "new_price_uf": 2690,
        },
    }})
    result = cli.prepare_selected(
        db, event=event, lead_ids=[str(lead_id)], operator="QA operator",
        contact_basis_reviewed=True, whatsapp_contact_reviewed=True,
        whatsapp_contact_evidence="Opt-in CRM reviewed 2026-10-01",
    )
    assert result["prepared_count"] == 1
    campaign = db["leads"].find_one({"_id": lead_id})["price_drop_v2"]["campaigns"][-1]
    assert campaign["price_reference"] == 2690
    assert [(row["old_price"], row["new_price"])
            for row in campaign["price_history_steps"]] == [(2690, 2590), (2590, 2490)]
    assert "cuando estaba en UF 2.690" in campaign["outbound_message"]


def test_event_and_message_history_dedupe_prevents_a_second_same_event():
    db = _event_db()
    event = discover_price_drops(db, now=NOW)[0]
    lead_id = _candidate(db, phone="+56983219801")
    db["leads"].update_one({"_id": lead_id}, {"$push": {"messages": {
        "role": "assistant", "content": "Aviso de precio UF 2.790 a UF 2.690 para propiedad 6436",
        "provider_message_id": "provider-existing", "delivery_status": "accepted",
    }}})
    candidate = find_candidates_for_event(db, event)[0]
    assert candidate["eligible"] is False
    assert candidate["reason"] == "prior_price_drop_message_needs_review"


def test_run_limit_waits_between_sends_and_leaves_remaining_approved(monkeypatch, tmp_path):
    db, _, ids = _dispatch_db(monkeypatch, count=3)
    waits, sent = [], []
    result = _run(db, max_messages=2, sleep_fn=waits.append,
                  sender=lambda phone, text: sent.append((phone, text)) or {
                      "success": True, "provider_message_id": f"mock-{phone[-4:]}",
                      "delivery_status": "accepted",
                  }, journal_path=tmp_path / "receipts.json")
    assert result["status"] == "LIMIT_REACHED"
    assert result["attempts"] == 2 and result["pending"] == 1
    assert len(sent) == 2 and waits == [450]
    statuses = [db["leads"].find_one({"_id": ObjectId(lead_id)})["price_drop_v2"]["campaigns"][0]["status"]
                for lead_id in ids]
    assert statuses.count("SENT") == 2 and statuses.count("APPROVED") == 1


def test_maximum_twenty_keeps_two_of_twenty_two_pending(monkeypatch, tmp_path):
    db, _, _ = _dispatch_db(monkeypatch, count=22)
    waits = []
    result = _run(db, max_messages=20, sleep_fn=waits.append,
                  journal_path=tmp_path / "twenty.json")
    assert result["status"] == "LIMIT_REACHED"
    assert result["attempts"] == 20 and result["pending"] == 2
    assert len(waits) == 19
    assert all(300 <= seconds <= 600 for seconds in waits)


def test_default_batch_has_no_wait_after_last_and_acceptance_is_in_chat_history(monkeypatch, tmp_path):
    db, _, ids = _dispatch_db(monkeypatch, count=3)
    waits = []
    result = _run(db, sleep_fn=waits.append, journal_path=tmp_path / "receipts.json")
    assert result["status"] == "COMPLETED" and result["attempts"] == 3
    assert waits == [450, 450]
    for lead_id in ids:
        lead = db["leads"].find_one({"_id": ObjectId(lead_id)})
        sent = lead["price_drop_v2"]["campaigns"][0]
        message = next(row for row in lead["messages"] if row.get("provider_message_id"))
        assert sent["status"] == "SENT"
        assert message["role"] == "assistant"
        assert message["content"] == sent["outbound_message"]
        assert message["metadata"]["tipo"] == "price_drop_outreach"
        assert message["metadata"]["campaign_id"] == CAMPAIGN
        assert message["delivery_status"] == "accepted"
    assert pending_receipts(tmp_path / "receipts.json") == []


def test_cancel_does_not_call_provider_and_unknown_delivery_stops(monkeypatch, tmp_path):
    db, _, _ = _dispatch_db(monkeypatch, count=3)
    calls = []
    cancelled = _run(db, confirm=lambda _: "no", sender=lambda *args: calls.append(args),
                     journal_path=tmp_path / "cancel.json")
    assert cancelled["status"] == "CANCELLED" and calls == []
    unknown = _run(db, sender=lambda *args: {"success": False},
                   journal_path=tmp_path / "unknown.json")
    assert unknown["status"] == "STOPPED_FOR_RECONCILIATION"
    assert unknown["attempts"] == 1 and unknown["pending"] == 2
    assert len(calls) == 0


def test_daily_cap_pause_interrupt_and_out_of_hours_are_safe(monkeypatch, tmp_path):
    db, _, _ = _dispatch_db(monkeypatch, count=3)
    limited = _run(db, daily_cap=1, journal_path=tmp_path / "daily.json")
    assert limited["status"] == "LIMIT_REACHED" and limited["attempts"] == 1
    db, _, _ = _dispatch_db(monkeypatch, count=2)
    def interrupt(_seconds):
        raise KeyboardInterrupt
    paused = _run(db, sleep_fn=interrupt, journal_path=tmp_path / "pause.json")
    assert paused["status"] == "INTERRUPTED_DURING_PAUSE"
    assert paused["attempts"] == 1 and paused["pending"] == 1
    db, _, _ = _dispatch_db(monkeypatch, count=2)
    closing_time = datetime(2026, 11, 9, 21, 55, tzinfo=timezone.utc)
    crossed = _run(db, now_fn=lambda: closing_time, rng=lambda _lo, _hi: 600,
                   journal_path=tmp_path / "closing.json")
    assert crossed["status"] == "STOPPED_OUTSIDE_BUSINESS_HOURS"
    assert crossed["attempts"] == 1 and crossed["pending"] == 1
    db, _, _ = _dispatch_db(monkeypatch, count=2)
    late = datetime(2026, 11, 9, 23, 30, tzinfo=timezone.utc)
    stopped = _run(db, now_fn=lambda: late, journal_path=tmp_path / "late.json")
    assert stopped["status"] == "BLOCKED" and stopped["reason"] == "outside_business_hours"


def test_progress_journal_survives_interrupt_and_resume_skips_accepted_lead(monkeypatch, tmp_path):
    db, _, ids = _dispatch_db(monkeypatch, count=3)
    progress = tmp_path / "progress.jsonl"
    calls = []

    def interrupt(_seconds):
        raise KeyboardInterrupt

    first = _run(
        db, sleep_fn=interrupt, progress_path=progress,
        sender=lambda phone, text: calls.append(phone) or {
            "success": True, "provider_message_id": f"accepted-{phone[-4:]}",
            "delivery_status": "accepted",
        }, journal_path=tmp_path / "pause-reconcile.json",
    )
    assert first["status"] == "INTERRUPTED_DURING_PAUSE"
    assert first["attempts"] == first["accepted"] == 1
    rows = [__import__("json").loads(line) for line in progress.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["attempts"] == 1
    assert rows[-1]["provider_accepted"] == 1
    assert rows[-1]["pending_total"] == 2

    second = _run(
        db, progress_path=progress,
        sender=lambda phone, text: calls.append(phone) or {
            "success": True, "provider_message_id": f"accepted-{phone[-4:]}",
            "delivery_status": "accepted",
        }, journal_path=tmp_path / "pause-reconcile.json",
    )
    assert second["status"] == "COMPLETED"
    assert second["attempts"] == 2
    assert len(calls) == 3
    assert len(set(calls)) == 3
    states = [db["leads"].find_one({"_id": ObjectId(value)})["price_drop_v2"]["campaigns"][0]["status"]
              for value in ids]
    assert states == ["SENT", "SENT", "SENT"]


def test_newer_drop_supersedes_only_unsent_campaigns_and_keeps_sent_rows():
    db = _event_db()
    old = discover_price_drops(db, now=NOW)[0]
    new_at = DROP + timedelta(days=5)
    _history(db, "6436", new_at, 2690, 2590)
    db["universo_cartera_prop360"].update_one({"codigo": "6436"}, {"$set": {
        "tipo_operacion.precio_venta.precio_uf": 2590,
        "tipo_operacion.precio_venta.precio_publicado": 2590,
    }})
    stale_id = _candidate(db, phone="+56983219801", extra={"price_drop_v2": {"campaigns": [{
        "campaign_id": old.event_id, "event_id": old.event_id, "property_code": "6436",
        "price_drop_at": old.dropped_at, "status": "APPROVED",
    }]}})
    sent_id = _candidate(db, phone="+56983219802", extra={"price_drop_v2": {"campaigns": [{
        "campaign_id": old.event_id, "event_id": old.event_id, "property_code": "6436",
        "price_drop_at": old.dropped_at, "status": "SENT",
    }]}})
    outcome = cli._supersede_stale_pending_campaigns(db, {"6436"}, now=NOW)
    assert outcome["count"] == 1
    stale = db["leads"].find_one({"_id": stale_id})["price_drop_v2"]["campaigns"][0]
    sent = db["leads"].find_one({"_id": sent_id})["price_drop_v2"]["campaigns"][0]
    assert stale["status"] == "SUPERSEDED"
    assert stale["superseded_by_event_id"] != old.event_id
    assert sent["status"] == "SENT"


def test_summary_groups_by_property_and_is_read_only(capsys):
    db = _event_db()
    event = discover_price_drops(db, now=NOW)[0]
    lead_id = _candidate(db, phone="+56983219801", temperature="HOT")
    db["leads"].update_one({"_id": lead_id}, {"$set": {"price_drop_v2": {
        "campaigns": [{
            "campaign_id": event.event_id, "event_id": event.event_id,
            "property_code": "6436", "currency": "UF",
            "old_price_uf": 2790, "new_price_uf": 2690,
            "price_drop_at": event.dropped_at, "status": "RESPONDED",
            "provider_message_id": "provider-accepted", "provider_status": "read",
            "sent_at": NOW - timedelta(hours=1), "reply_count": 1,
            "has_positive_response": True,
            "last_classification": {"label": "PRIORIDAD_COMERCIAL", "visit_requested": True},
        }],
    }}})
    before = db["leads"].find_one({"_id": lead_id})
    result = cli._summary_report(
        db, temperature_filter="HOT", lead_max_age_days=120, now=NOW,
    )
    after = db["leads"].find_one({"_id": lead_id})
    output = capsys.readouterr().out
    assert result["accepted"] == result["delivered"] == result["read"] == 1
    assert result["responses"] == result["interested"] == result["visits"] == 1
    assert "6436" in output and "UF 2.790 → UF 2.690" in output
    assert before == after


def test_summary_cli_is_read_only_and_period_does_not_hide_current_drop(monkeypatch, capsys):
    db = _event_db()
    before = list(db["leads"].find({}))
    monkeypatch.setattr(cli, "_get_db", lambda: db)
    assert cli.main(["--summary", "--period-days", "1", "--temperature", "HOT,NORMAL"]) == 0
    output = capsys.readouterr().out
    assert "6436" in output and "Rebaja vigente" in output
    assert list(db["leads"].find({})) == before


def test_limits_and_link_review_fail_closed(monkeypatch, tmp_path):
    with pytest.raises(cli.CliError, match="max_messages"):
        cli.validate_limits(0, 300, 600)
    with pytest.raises(cli.CliError, match="not_be_negative"):
        cli.validate_limits(3, -1, 600)
    with pytest.raises(cli.CliError, match="at_least_60"):
        cli.validate_limits(3, 5, 600, provider_plan="trial")
    with pytest.raises(cli.CliError, match="daily_cap_must_not_exceed_50"):
        cli.validate_limits(3, 300, 600, provider_plan="trial", daily_cap=51)
    db, _, _ = _dispatch_db(monkeypatch, count=2)
    result = _run(db, link_verifier=lambda code, now: False,
                  journal_path=tmp_path / "link.json")
    assert result["status"] == "BLOCKED_RECIPIENTS_PENDING"
    assert result["attempts"] == 0 and result["pending"] == 2


def test_wasender_allows_authorized_plain_text_outside_24_hours(monkeypatch, tmp_path):
    db, _, _ = _dispatch_db(monkeypatch, count=1)
    monkeypatch.setenv("PRICE_DROP_WHATSAPP_CHANNEL_MODE", "wasenderapi")
    db["leads"].update_one({}, {"$set": {
        "messages.1.timestamp": (DROP - timedelta(days=10)).isoformat(),
    }})
    entry = cli._entries(db, CAMPAIGN, ("APPROVED",))[0]
    assert cli._message_window_blocker(entry, NOW) is None
    result = _run(db, journal_path=tmp_path / "wasender-old-inbound.json")
    assert result["status"] == "COMPLETED" and result["attempts"] == 1


def test_official_channel_keeps_24_hour_rule_and_is_not_routed_to_wasender(monkeypatch, tmp_path):
    db, _, _ = _dispatch_db(monkeypatch, count=1)
    monkeypatch.setenv("PRICE_DROP_WHATSAPP_CHANNEL_MODE", "official")
    db["leads"].update_one({}, {"$set": {
        "messages.1.timestamp": (DROP - timedelta(days=10)).isoformat(),
    }})
    entry = cli._entries(db, CAMPAIGN, ("APPROVED",))[0]
    assert cli._message_window_blocker(entry, NOW) == \
        "whatsapp_24_hour_window_expired_template_required"
    result = _run(db, journal_path=tmp_path / "official.json")
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "official_channel_sender_not_implemented"


def test_reconciliation_restores_accepted_message_without_duplicate_echo(monkeypatch, tmp_path):
    db, _, ids = _dispatch_db(monkeypatch, count=1)
    lead_id = ObjectId(ids[0])
    db["leads"].update_one({"_id": lead_id}, {"$set": {
        "price_drop_v2.campaigns.0.status": "SENDING",
        "price_drop_v2.campaigns.0.attempted_at": NOW,
    }})
    journal = tmp_path / "reconcile.json"
    record_accepted_receipt(journal, lead_id=ids[0], campaign_id=CAMPAIGN,
                            provider_message_id="wa-accepted-1", accepted_at=NOW)
    result = reconcile_pending_receipts(db, journal, now=NOW)
    assert result["reconciled"] == 1 and result["unresolved"] == []
    lead = db["leads"].find_one({"_id": lead_id})
    assert lead["price_drop_v2"]["campaigns"][0]["status"] == "SENT"
    assert len(lead["messages"]) == 3
    assert lead["messages"][-1]["provider_message_id"] == "wa-accepted-1"

    db, _, ids = _dispatch_db(monkeypatch, count=1)
    lead_id = ObjectId(ids[0])
    db["leads"].update_one({"_id": lead_id}, {"$set": {
        "price_drop_v2.campaigns.0.status": "SENDING",
        "price_drop_v2.campaigns.0.attempted_at": NOW,
    }, "$push": {"messages": {
        "role": "assistant", "content": "accepted echo", "provider_message_id": "wa-echo-1",
    }}})
    journal = tmp_path / "echo.json"
    record_accepted_receipt(journal, lead_id=ids[0], campaign_id=CAMPAIGN,
                            provider_message_id="wa-echo-1", accepted_at=NOW)
    result = reconcile_pending_receipts(db, journal, now=NOW)
    lead = db["leads"].find_one({"_id": lead_id})
    assert result["reconciled"] == 1
    assert len([row for row in lead["messages"] if row.get("provider_message_id") == "wa-echo-1"]) == 1


def test_context_and_reply_classification_use_existing_v2_policy(monkeypatch, tmp_path):
    db, _, ids = _dispatch_db(monkeypatch, count=1)
    _run(db, journal_path=tmp_path / "context.json")
    monkeypatch.setenv(v2.CONTEXT_FLAG, "true")
    phone = db["leads"].find_one({"_id": ObjectId(ids[0])})["phone"]
    assert v2.active_context(db, phone=phone, message="me interesa",
                             now=NOW + timedelta(days=3))["campaign_id"] == CAMPAIGN
    assert v2.active_context(db, phone=phone, message="hola",
                             now=NOW + timedelta(days=8)) is None
    assert any(row.get("metadata", {}).get("campaign_id") == CAMPAIGN
               for row in db["leads"].find_one({"_id": ObjectId(ids[0])})["messages"])
    assert classify_price_drop_reply("sí, me interesa").label == "INTERES_DECLARADO"
    assert classify_price_drop_reply("¿cuánto cuesta?").label == "CONSULTA_INFORMATIVA"
    assert classify_price_drop_reply("quiero visitar").explicit_visit is True
    assert classify_price_drop_reply("no me interesa").label == "RECHAZADO"
    assert classify_price_drop_reply("no me escriban más").label == "BAJA"


@pytest.mark.parametrize("message", [
    "¿Me puedes mostrar otras propiedades en Ñuñoa?",
    "¿Podrías buscar opciones en La Reina?",
    "¿Pueden buscar propiedades en otra comuna?",
    "No me interesa esta propiedad, ¿pueden buscar opciones en La Reina?",
    "Estoy buscando algo por Macul en vez de esta comuna.",
])
def test_alternative_search_requests_are_not_misclassified_as_information_or_rejection(message):
    result = classify_price_drop_reply(message)
    assert result.label == "BUSCA_ALTERNATIVAS"
    assert result.alternatives_requested is True


@pytest.mark.parametrize("message", [
    "No busco otras propiedades, solo quiero saber el precio de esta.",
    "No me interesa otra comuna; ¿cuánto cuestan los gastos comunes de esta?",
    "Vivo en Ñuñoa y quisiera saber el precio de esta propiedad.",
])
def test_mentions_of_other_properties_or_communes_need_affirmative_search_intent(message):
    result = classify_price_drop_reply(message)
    assert result.label != "BUSCA_ALTERNATIVAS"
    assert result.alternatives_requested is False


@pytest.mark.parametrize("message", [
    "¿Cómo queda el precio del código 12000 después de la rebaja?",
    "¿Cuál es el valor vigente de la propiedad 12001 ahora?",
    "Vi que rebajó el código 54321; ¿qué precio tiene actualmente?",
])
def test_property_codes_and_price_questions_are_not_false_offer_intents(message):
    result = classify_price_drop_reply(message)
    assert result.explicit_offer is False
    assert result.intent == "ASK_INFO"


def test_call_preference_is_not_global_opt_out_or_call_request():
    result = classify_price_drop_reply(
        "Sí me interesa, pero prefiero que no me contacten por teléfono."
    )
    assert result.label == "INTERES_DECLARADO"
    assert result.do_not_call is True
    assert result.explicit_contact is False


@pytest.mark.parametrize("message", [
    "¿Puede contactarme un asesor para revisar esta propiedad?",
    "¿Podrían escribirme primero por este chat?",
    "Necesito conversar con una persona del equipo.",
])
def test_explicit_human_contact_requests_are_recognized(message):
    result = classify_price_drop_reply(message)
    assert result.label == "PRIORIDAD_COMERCIAL"
    assert result.intent == "ASK_CONTACT"
    assert result.explicit_contact is True


def test_offer_amount_requires_an_offer_verb_and_stays_a_priority_intent():
    positive = classify_price_drop_reply("¿Puedo hacer una oferta condicionada a financiamiento?")
    negative = classify_price_drop_reply("¿Cuánto cuesta la propiedad código 12000?")
    assert positive.intent == "GIVE_OFFER"
    assert positive.explicit_offer is True
    assert negative.explicit_offer is False


def test_rejecting_a_property_while_requesting_another_is_not_stored_as_rejection():
    result = classify_price_drop_reply(
        "No quiero esta propiedad. ¿Podrían recomendarme otra en La Reina?"
    )
    assert result.label == "BUSCA_ALTERNATIVAS"
    assert result.alternatives_requested is True


@pytest.mark.parametrize(("message", "expected"), [
    ("¿Puedo visitar esta propiedad este fin de semana?", "agendar_visita"),
    ("¿Qué día podría ir a verla?", "agendar_visita"),
    ("¿Podrían escribirme primero por este chat?", "contacto_directo"),
    ("También necesito conversar con una persona del equipo.", "contacto_directo"),
    ("Sí me interesa, pero no quiero que me llamen.", None),
    ("No me contacten por teléfono, por favor.", None),
    ("No me interesa visitar por ahora; ¿cuáles son los gastos comunes?", None),
])
def test_campaign_operational_intent_requires_explicit_non_negated_request(message, expected):
    from chatbot.price_drop_campaign_classifier import campaign_operational_core_intent
    assert campaign_operational_core_intent(message) == expected


def test_acceptance_price_evaluator_distinguishes_price_change_from_expenses():
    from scripts.run_price_drop_deepseek_acceptance import _verify_listing_price_claims

    verified = _verify_listing_price_claims(
        "La rebaja fue de $8.000.000: pasó de $136.000.000 a $128.000.000; "
        "los gastos comunes son $75.000.",
        currency="CLP", old_price=136_000_000, new_price=128_000_000,
    )
    assert all(claim["verified"] for claim in verified)
    assert [claim["kind"] for claim in verified].count("listing_price") == 3
    assert [claim["kind"] for claim in verified].count("non_listing_amount") == 1

    incorrect = _verify_listing_price_claims(
        "El precio actual es $127.000.000.",
        currency="CLP", old_price=136_000_000, new_price=128_000_000,
    )
    assert incorrect[0]["verified"] is False


def test_acceptance_property_fact_evaluator_uses_listing_variation():
    from scripts.run_price_drop_deepseek_acceptance import _property_numeric_fact_mismatches

    correct = (
        "El departamento tiene 3 dormitorios y 2 baños, con 55 m² útiles "
        "y 60 m² totales, orientación oriente, y 0 estacionamientos."
    )
    incorrect = correct.replace("3 dormitorios", "4 dormitorios")
    assert _property_numeric_fact_mismatches(correct, variation=10) == []
    assert _property_numeric_fact_mismatches(incorrect, variation=10) == [
        "bedrooms_claim_mismatch"
    ]


def test_acceptance_visit_guard_does_not_mistake_negative_acknowledgment_for_offer():
    from scripts.run_price_drop_deepseek_acceptance import _contains_visit_invitation

    assert not _contains_visit_invitation(
        "Entiendo que por ahora no quieres coordinar una visita; quedo atenta."
    )
    assert _contains_visit_invitation(
        "Si quieres, podemos coordinar una visita cuando estés disponible."
    )


def test_v2_price_drop_prompt_requests_json_and_retries_a_non_json_reply(monkeypatch):
    import json
    from types import SimpleNamespace
    from config import Config
    monkeypatch.setattr(Config, "DEEPSEEK_API_KEY", "unit-test-key-not-used")
    from chatbot import grok_client

    calls = []
    outputs = [
        "La propiedad está en Ñuñoa y bajó de precio.",
        json.dumps({
            "intencion": "consulta_general",
            "datos_extraidos": {},
            "respuesta_bot": "El precio publicado es UF 2.800.",
        }),
    ]

    def fake_create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=outputs.pop(0)),
                finish_reason="stop",
            )],
            usage=None,
        )

    monkeypatch.setattr(grok_client.client.chat.completions, "create", fake_create)
    monkeypatch.setattr(grok_client, "_record_llm_telemetry", lambda **_kwargs: None)
    monkeypatch.setattr(grok_client.Config, "DEEPSEEK_RESPONSE_FORMAT", "text")
    diagnostics = {}
    result = grok_client.generar_respuesta_estructurada(
        [
            {"role": "system", "content": (
                "[PILOTO DE REACTIVACIÓN POR REBAJA - CONTEXTO VERIFICADO]"
            )},
            {"role": "user", "content": "¿Cuál es el precio actual?"},
        ],
        {},
        write_telemetry=True,
        diagnostics=diagnostics,
    )

    assert calls[0]["response_format"] == {"type": "json_object"}
    assert len(calls) == 2
    assert calls[1]["response_format"] == {"type": "json_object"}
    assert result["respuesta_bot"] == "El precio publicado es UF 2.800."
    assert diagnostics["format_retries"] == 1
    assert "error" not in diagnostics


def test_webhook_outbound_echo_is_ignored_by_existing_ingress_policy():
    from chatbot.webhook_event_policy import is_outbound_message_event
    assert is_outbound_message_event({"event": "message.sent"}) is True
    assert is_outbound_message_event({"data": {"event": "messages.sent"}}) is True
    assert is_outbound_message_event({"event": "message.received"}) is False


class _HttpResponse:
    def __init__(self, status_code=200, body=b"", payload=None, headers=None):
        self.status_code = status_code
        self.content = body
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


def _listing_check(db, html, *, status=200):
    return verify_current_listing(
        db, property_code="6436", currency="UF", expected_price=2690,
        expected_url="https://www.procasa.cl/propiedad/6436", now=NOW,
        http_get=lambda *_args, **_kwargs: _HttpResponse(status, html.encode("utf-8")),
    )


def test_public_listing_verification_distinguishes_verified_unknown_and_mismatch():
    db = _event_db()
    matching = ('<meta property="product:price:amount" content="2690">'
                '<meta property="product:price:currency" content="UF">')
    verified = _listing_check(db, matching)
    assert verified.status == "price_verified" and verified.sendable is True
    mismatch = matching.replace('content="2690"', 'content="2790"')
    inconsistent = _listing_check(db, mismatch)
    assert inconsistent.status == "inventory_public_inconsistent"
    assert inconsistent.sendable is False
    unknown = _listing_check(db, "<html><title>Departamento</title></html>")
    assert unknown.status == "link_accessible_price_unverifiable"
    assert unknown.sendable is False


def test_public_listing_verification_distinguishes_removed_and_inaccessible():
    db = _event_db()
    removed = _listing_check(db, "", status=404)
    assert removed.status == "property_removed"
    assert removed.sendable is False
    inaccessible = verify_current_listing(
        db, property_code="6436", currency="UF", expected_price=2690,
        http_get=lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
        now=NOW,
    )
    assert inaccessible.status == "link_inaccessible"
    assert inaccessible.sendable is False


def test_wasender_usage_counts_recent_api_logs_across_all_sessions():
    recent = (NOW - timedelta(hours=2)).isoformat()
    old = (NOW - timedelta(hours=30)).isoformat()
    calls = []

    def get(url, *, params=None, headers=None, **_kwargs):
        calls.append((url, params, headers))
        if url.endswith("/whatsapp-sessions"):
            return _HttpResponse(payload={"success": True, "data": [
                {"id": "session-1", "log_messages": True},
                {"id": "session-2", "log_messages": True},
            ]})
        session = url.split("/whatsapp-sessions/")[1].split("/")[0]
        if not url.endswith("/message-logs"):
            return _HttpResponse(payload={"success": True, "data": {
                "id": session, "api_key": f"{session}-key", "log_messages": True,
            }})
        page = params["page"]
        assert headers["Authorization"] == f"Bearer {session}-key"
        if session == "session-1" and page == 1:
            data = [{"id": "1", "created_at": recent, "status": "sent"},
                    {"id": "2", "created_at": old, "status": "failed"}]
            last_page = 2
        elif session == "session-1":
            data, last_page = [], 2
        else:
            data = [{"id": "3", "created_at": recent, "status": "in_progress"}]
            last_page = 1
        return _HttpResponse(payload={"success": True, "data": {
            "current_page": page, "last_page": last_page, "data": data,
        }})

    result = account_attempts_last_24h(
        now=NOW, base_url="https://wasenderapi.com/api",
        personal_access_token="account-pat", http_get=get,
    )
    assert result["attempts_last_24h"] == 2
    assert result["sessions_scanned"] == 2
    assert result["pages_scanned"] == 3
    assert len(calls) == 6
    assert calls[0][2]["Authorization"] == "Bearer account-pat"
    assert all(
        call[2]["Authorization"] == (
            "Bearer account-pat" if not call[0].endswith("/message-logs")
            else "Bearer " + call[0].split("/whatsapp-sessions/")[1].split("/")[0] + "-key"
        )
        for call in calls
    )


def test_wasender_usage_fails_closed_when_any_session_logs_are_disabled():
    def get(url, **_kwargs):
        return _HttpResponse(payload={"success": True, "data": [
            {"id": "session-1", "log_messages": False},
            {"id": "session-2", "log_messages": True},
        ]})

    with pytest.raises(WaSenderUsageUnavailable, match="logs_disabled"):
        account_attempts_last_24h(
            now=NOW, base_url="https://wasenderapi.com/api",
            personal_access_token="account-pat", http_get=get,
        )


def test_account_usage_uses_personal_access_token_not_session_key(monkeypatch):
    from config import Config

    monkeypatch.setattr(Config, "WASENDER_TOKEN", "session-api-key", raising=False)
    monkeypatch.setattr(Config, "WASENDER_PERSONAL_ACCESS_TOKEN", "account-pat", raising=False)
    monkeypatch.setattr(Config, "WASENDER_BASE_URL", "https://wasenderapi.com/api", raising=False)
    captured = {}

    def usage_reader(**kwargs):
        captured.update(kwargs)
        return {"attempts_last_24h": 0}

    monkeypatch.setattr(cli, "account_attempts_last_24h", usage_reader)
    assert cli._provider_attempts_last_24h(now=NOW) == {"attempts_last_24h": 0}
    assert captured["personal_access_token"] == "account-pat"
    assert captured["personal_access_token"] != Config.WASENDER_TOKEN


def test_account_usage_does_not_fall_back_to_session_key_when_pat_missing(monkeypatch):
    from config import Config

    monkeypatch.setattr(Config, "WASENDER_TOKEN", "session-api-key", raising=False)
    monkeypatch.setattr(Config, "WASENDER_PERSONAL_ACCESS_TOKEN", None, raising=False)
    monkeypatch.setattr(Config, "WASENDER_BASE_URL", "https://wasenderapi.com/api", raising=False)

    with pytest.raises(cli.CliError, match="personal_access_token_required"):
        cli._provider_attempts_last_24h(now=NOW)


def test_interactive_menu_previews_then_prepares_approves_and_sends_mock_only(tmp_path):
    db = _event_db()
    lead_id = _candidate(db, phone="+56983219801", temperature="NORMAL")
    answers = iter([
        "TODOS", "120", "1", "1", "1", "300", "600", "QA operator",
        "CRM opt-in reviewed ref 2026-11-01", "CONFIRMO 1", "PREPARAR 1",
        "APROBAR 1", "ENVIAR 1",
    ])
    sent = []
    check = PublicationCheck("price_verified", True, "6436", "https://www.procasa.cl/propiedad/6436",
                             200, 2690, "UF", 2690, "UF")
    result = cli.interactive_workflow(
        db, input_fn=lambda _prompt: next(answers), output_fn=lambda _line: None,
        sender=lambda phone, body: sent.append((phone, body)) or {
            "success": True, "provider_message_id": "mock-menu-1", "delivery_status": "accepted",
        },
        usage_reader=lambda _now: 0, provider_ready=lambda: True,
        publication_verifier=lambda *_args, **_kwargs: check,
        backup_fn=lambda *_args: tmp_path / "backup.json",
        now_fn=lambda: NOW, journal_path=tmp_path / "menu.json",
    )
    lead = db["leads"].find_one({"_id": lead_id})
    campaign = lead["price_drop_v2"]["campaigns"][0]
    assert result["status"] == "COMPLETED" and result["attempts"] == 1, result
    assert campaign["status"] == "SENT"
    assert len(sent) == 1 and sent[0][1] == campaign["outbound_message"]
    assert lead["messages"][-1]["provider_message_id"] == "mock-menu-1"


def test_interactive_cancel_before_prepare_keeps_mongo_read_only():
    db = _event_db()
    lead_id = _candidate(db, phone="+56983219801", temperature="HOT")
    answers = iter([
        "TODOS", "120", "1", "1", "1", "300", "600", "QA operator",
        "CRM opt-in reviewed ref 2026-11-01", "CONFIRMO 1", "NO",
    ])
    check = PublicationCheck("price_verified", True, "6436", "https://www.procasa.cl/propiedad/6436",
                             200, 2690, "UF", 2690, "UF")
    result = cli.interactive_workflow(
        db, input_fn=lambda _prompt: next(answers), output_fn=lambda _line: None,
        publication_verifier=lambda *_args, **_kwargs: check, now_fn=lambda: NOW,
    )
    lead = db["leads"].find_one({"_id": lead_id})
    assert result["status"] == "CANCELLED"
    assert not lead.get("price_drop_v2")
    assert len(lead["messages"]) == 1


def test_full_portfolio_audit_counts_all_events_without_writes_or_provider_calls(monkeypatch, capsys):
    """Two price drops, one lead interested in both: report overlap not bulk send."""
    import json

    db = _event_db()
    _seed_property(db, code="6377", currency="UF", current=5074)
    _history(db, "6377", DROP, 5123, 5074)
    lead_id = _candidate(
        db, phone="+56983219801", temperature="NORMAL",
        content="Me interesa la propiedad código 6436",
    )
    db["leads"].update_one({"_id": lead_id}, {"$push": {"messages": {
        "role": "user", "content": "También consulté por la propiedad 6377",
        "timestamp": (DROP - timedelta(days=1)).isoformat(),
    }}})
    _candidate(db, phone="+56983219802", temperature="COLD",
               content="Me interesa la propiedad 6377")
    before = list(db["leads"].find())
    report = cli.audit_full_portfolio(
        db, now=NOW, temperature_filter="ALL", lead_max_age_days=None,
    )
    assert report["database_mode"] == "READ_ONLY"
    assert report["send_approved"] is False
    assert report["totals"]["events_found"] == 2
    assert report["totals"]["property_codes"] == 2
    assert report["totals"]["eligible_event_matches"] == 3
    assert report["totals"]["unique_eligible_leads"] == 2
    assert report["totals"]["cross_event_duplicate_matches"] == 1
    assert report["reviewed_whatsapp_authorizations"] == "NOT_VERIFIED_BY_THIS_AUDIT"
    assert list(db["leads"].find()) == before
    monkeypatch.setattr(cli, "_get_db", lambda: db)
    assert cli.main(["--audience-audit", "--full-scan",
                     "--lead-max-age-days", "0"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["totals"]["eligible_event_matches"] == 3
    assert parsed["sending_attempts"] == 0
    assert list(db["leads"].find()) == before


def test_full_portfolio_audit_reports_missing_temperature_review_without_approving_it():
    db = _event_db()
    _candidate(db, phone="+56983219801", temperature="UNKNOWN")
    report = cli.audit_full_portfolio(db, now=NOW, lead_max_age_days=None)
    assert report["totals"]["eligible_event_matches"] == 0
    assert report["totals"]["requires_temperature_review"] == 1
    assert report["totals"]["excluded_or_review_needed"] == 1
    assert report["send_approved"] is False


def test_full_portfolio_wave_deduplicates_lead_across_two_price_drops():
    db = _event_db()
    _seed_property(db, code="6377", currency="UF", current=5074)
    _history(db, "6377", DROP + timedelta(days=1), 5123, 5074)
    events = discover_price_drops(db, now=NOW)
    assert len(events) == 2
    newer = max(events, key=lambda event: event.dropped_at)
    older = min(events, key=lambda event: event.dropped_at)
    choices = {
        older.event_id: (older, [{"lead_id": "one"}, {"lead_id": "two"}]),
        newer.event_id: (newer, [{"lead_id": "one"}]),
    }
    deduped, suppressed = cli.deduplicate_wave_recipients(choices)
    assert suppressed == 1
    assert [c["lead_id"] for c in deduped[older.event_id][1]] == ["two"]
    assert [c["lead_id"] for c in deduped[newer.event_id][1]] == ["one"]
    assert len({row["lead_id"] for _, rows in deduped.values() for row in rows}) == 2
