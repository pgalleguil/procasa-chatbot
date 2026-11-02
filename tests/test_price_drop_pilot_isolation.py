"""End-to-end isolated handler tests: no CRM/SLA collections are available."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pymongo.errors import DuplicateKeyError

from chatbot.price_drop_pilot_isolation import (
    build_test_reply, is_test_phone, load_test_context, process_test_inbound,
)

PHONE = "+56977770000"


class MemoryCursor:
    def __init__(self, rows):
        self.rows = list(rows)
    def sort(self, name, direction):
        self.rows.sort(key=lambda row: row.get(name, datetime.min.replace(tzinfo=timezone.utc)), reverse=direction == -1)
        return self
    def limit(self, count):
        self.rows = self.rows[:count]
        return self
    def __iter__(self):
        return iter(self.rows)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.calls = []
    def find_one(self, query, projection=None, sort=None):
        self.calls.append(("find_one", query))
        if sort and self.docs:
            return sorted(self.docs, key=lambda x: x.get("sent_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[0]
        return self.docs[0] if self.docs else None
    def find(self, query, projection=None):
        self.calls.append(("find", query))
        return MemoryCursor([x for x in self.docs if
                             x.get("campaign_id") == query.get("campaign_id")
                             and x.get("status") == query.get("status")
                             and x.get("_id") != query.get("_id", {}).get("$ne")])
    def insert_one(self, doc):
        self.calls.append(("insert_one", doc["_id"]))
        if any(x["_id"] == doc["_id"] for x in self.docs):
            raise DuplicateKeyError("duplicate test provider id")
        self.docs.append(dict(doc))
    def update_one(self, selector, update):
        self.calls.append(("update_one", selector))
        for doc in self.docs:
            if doc.get("_id") == selector.get("_id"):
                doc.update(update.get("$set", {}))
                return


class BlockedCollection:
    def __getattr__(self, name):
        raise AssertionError("Live CRM collection was touched: " + name)


class FakeDB(dict):
    def __missing__(self, key):
        if key in {"leads", "crm_assignment_cycles", "crm_management_results",
                   "event_log", "chatbot_message_queue", "crm_sla_alerts"}:
            raise AssertionError("Unexpected CRM/SLA collection: " + key)
        raise AssertionError("Unexpected collection: " + key)


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setenv("PRICE_DROP_PILOT_ENABLED", "1")
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", PHONE)


def db_case(*, duplicate_live_identity=False, active=True, price=2690):
    now = datetime.now(timezone.utc)
    campaign = {
        "_id": "test:6436",
        "phone_normalized": PHONE, "test_mode": True,
        "provider_message_id": "provider-original",
        "property_code": "6436",
        "operation": "Venta",
        "old_price_uf": 2790.0, "new_price_uf": 2690.0,
        "status": "SENT", "sent_at": now - timedelta(minutes=5),
        "expires_at": now + timedelta(days=1),
    }
    property_doc = {
        "codigo": "6436",
        "disponible_prop360": active,
        "estado": {"oficina": "PROCASA SUCRE"},
        "tipo_operacion": {"venta": True, "precio_venta": {"precio_uf": price}},
    }
    db = FakeDB({
        "crm_price_drop_pilot_v1": FakeCollection([campaign]),
        "universo_cartera_prop360": FakeCollection([property_doc]),
        "crm_price_drop_pilot_test_turns_v1": FakeCollection(),
    })
    # Simulate two previously existing records. They are never read.
    db["leads"] = BlockedCollection()
    return db


def sender_spy():
    calls = []
    def send(phone, answer):
        calls.append((phone, answer))
        return {"success": True, "provider_message_id": "reply-ack-" + str(len(calls))}
    return send, calls


def test_isolated_yes_no_executive_notifications_or_crm_changes(isolated):
    db = db_case()
    sender, calls = sender_spy()
    result = process_test_inbound(db, PHONE, "incoming-yes-1", "Sí", sender=sender)
    assert result == {"status": "test_reply_sent"}
    assert len(calls) == 1
    assert "6436" in calls[0][1]
    assert db["crm_price_drop_pilot_test_turns_v1"].docs[0]["status"] == "SENT"
    assert set(db.keys()) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1"
    }


@pytest.mark.parametrize("message,fragment", [
    ("Sí, me interesa", "6436"),
    ("Quiero visitarla", "¿Qué día"),
    ("¿Cuánto cuesta ahora?", "2.690"),
    ("¿Aceptan ofertas?", "oferta"),
    ("No me interesa", "No insistiremos"),
    ("¿Tienen otra en Ñuñoa?", "otras"),
    ("6500", "6436"),
])
def test_intents_without_commercial_actions(isolated, message, fragment):
    db = db_case()
    sender, calls = sender_spy()
    result = process_test_inbound(db, PHONE, "in-" + message, message, sender=sender)
    assert result["status"] == "test_reply_sent"
    assert fragment.casefold() in calls[0][1].casefold()


def test_freeform_reply_runs_isolated_model_not_crm(isolated):
    db = db_case()
    sender, calls = sender_spy()
    seen = []
    def llm(message, ctx, history):
        seen.append((message, ctx, history))
        return "La ficha de esta prueba no indica si incluye bodega."
    result = process_test_inbound(
        db, PHONE, "question-1", "¿Tiene conserjería?", sender=sender, llm=llm
    )
    assert result["status"] == "test_reply_sent"
    assert seen[0][1]["property_code"] == "6436"
    assert calls[0][1].startswith("La ficha")


def test_duplicate_provider_event_never_resends(isolated):
    db = db_case()
    sender, calls = sender_spy()
    process_test_inbound(db, PHONE, "same-id", "Sí", sender=sender)
    assert process_test_inbound(db, PHONE, "same-id", "Sí", sender=sender) == {
        "status": "duplicate_suppressed",
    }
    assert len(calls) == 1


def test_stop_never_sends_or_mutates_crm(isolated):
    db = db_case()
    sender, calls = sender_spy()
    outcome = process_test_inbound(db, PHONE, "stop-1", "No me escriban más", sender=sender)
    assert outcome == {"status": "test_stopped"}
    assert not calls
    assert db["crm_price_drop_pilot_v1"].docs[0]["status"] == "STOPPED"
    assert process_test_inbound(db, PHONE, "stop-2", "Sí", sender=sender) == {
        "status": "test_not_active",
    }


def test_inactive_campaign_never_falls_through_or_sends(isolated):
    db = db_case()
    db["crm_price_drop_pilot_v1"].docs[0]["status"] = "SENDING"
    sender, calls = sender_spy()
    assert process_test_inbound(db, PHONE, "no-campaign", "sí", sender=sender) == {
        "status": "test_not_active",
    }
    assert not calls


def test_unverified_price_never_sends(isolated):
    db = db_case(price=2800)
    sender, calls = sender_spy()
    assert process_test_inbound(db, PHONE, "bad-price", "Sí", sender=sender)["status"] == "test_not_active"
    assert not calls


def test_outside_phone_never_reads_or_sends(isolated):
    db = db_case()
    sender, calls = sender_spy()
    assert process_test_inbound(db, "+56977770001", "someone", "Sí", sender=sender)["status"] == "not_test_phone"
    assert not calls


def test_provider_failure_has_no_retry(isolated):
    db = db_case()
    calls = []
    def bad_sender(*args):
        calls.append(1)
        raise TimeoutError()
    assert process_test_inbound(db, PHONE, "failure-1", "sí", sender=bad_sender)["status"] == "delivery_unknown"
    assert process_test_inbound(db, PHONE, "failure-1", "sí", sender=bad_sender)["status"] == "duplicate_suppressed"
    assert calls == [1]


def test_model_cannot_claim_executive_was_notified(isolated):
    ctx = {"property_code": "6436", "old_price_uf": 2790, "new_price_uf": 2690}
    answer = build_test_reply("¿Hay conserjería?", ctx,
                              llm=lambda *_: "He notificado al ejecutivo y te llamará")
    assert "he notificado" not in answer.lower()
    assert "no notificaremos" in answer.lower()


def test_webhook_interception_occurs_before_production_operations():
    source = Path(__file__).resolve().parents[1].joinpath("webhook.py").read_text()
    marker = source.index("    # Isolated price-drop test: intercept this exact phone BEFORE log_event")
    assert marker < source.index("    # Para el log de eventos, usamos el número limpio sin el '+'", marker)
    assert marker < source.index("    # Persist to the durable queue.", marker)
    assert 'if is_test_phone(phone):' in source[marker:marker + 1600]


def test_script_no_longer_looks_up_or_edits_existing_leads():
    source = Path(__file__).resolve().parents[1].joinpath("scripts/price_drop_pilot_test.py").read_text()
    assert 'db["leads"]' not in source
    assert '"test_mode": True' in source
    assert "if not send:" in source
    assert "return run_test_once(send=args.send)" in source


def test_bootstrap_one_shot_is_disabled_by_default():
    source = Path(__file__).resolve().parents[1].joinpath("webhook.py").read_text()
    assert 'if str(os.getenv("PRICE_DROP_PILOT_SEND_ON_START", "")).lower()' in source
    assert 'lambda: run_test_once(send=True)' in source


def test_pilot_sender_once_and_never_writes_existing_leads(isolated, monkeypatch):
    """Dry-run cannot send; the first live call is recorded; second cannot send."""
    from scripts import price_drop_pilot_test as sender_script
    db = db_case()
    test_collection = db["crm_price_drop_pilot_v1"]
    test_collection.docs = []  # no prior send record

    monkeypatch.setattr(sender_script, "get_db", lambda: db)
    calls = []
    def stub_send(phone, text):
        calls.append((phone, text))
        return {"success": True, "provider_message_id": "provider-test-sent"}
    monkeypatch.setattr(sender_script, "send_whatsapp_message_detailed_sync", stub_send)

    assert sender_script.run_test_once(send=False) == 0
    assert not calls
    assert not test_collection.docs

    assert sender_script.run_test_once(send=True) == 0
    assert len(calls) == 1
    assert test_collection.docs[0]["test_mode"] is True
    assert "lead_id" not in test_collection.docs[0]
    assert test_collection.docs[0]["status"] == "SENT"
    assert load_test_context(db, PHONE)["property_code"] == "6436"

    assert sender_script.run_test_once(send=True) == 2
    assert len(calls) == 1


def test_failed_provider_attempt_does_not_auto_retry(isolated, monkeypatch):
    from scripts import price_drop_pilot_test as sender_script
    db = db_case()
    db["crm_price_drop_pilot_v1"].docs = []
    monkeypatch.setattr(sender_script, "get_db", lambda: db)
    calls = []
    def failed_send(phone, text):
        calls.append(phone)
        raise TimeoutError("unknown")
    monkeypatch.setattr(sender_script, "send_whatsapp_message_detailed_sync", failed_send)
    assert sender_script.run_test_once(send=True) == 1
    assert db["crm_price_drop_pilot_v1"].docs[0]["status"] == "DELIVERY_UNKNOWN"
    assert sender_script.run_test_once(send=True) == 2
    assert len(calls) == 1



def test_prop360_real_features_are_included_in_verified_context(isolated):
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["caracteristicas"] = {
        "dormitorios": 2, "banos": 2, "bodegas": 1,
        "superficie_total": 65, "superficie_util": 60,
    }
    db["universo_cartera_prop360"].docs[0]["tipo_operacion"]["gastos_comunes"] = 85000
    ctx = load_test_context(db, PHONE)
    assert ctx["property_facts"] == {
        "dormitorios": 2, "banos": 2, "bodegas": 1,
        "superficie_total": 65.0, "superficie_util": 60.0,
        "gastos_comunes_clp": 85000,
    }
    assert "2 baños" in build_test_reply("¿Cuántos baños tiene?", ctx)
    assert "2 dormitorios" in build_test_reply("¿Cuántos dormitorios tiene?", ctx)
    assert "85.000" in build_test_reply("¿Cuánto son los gastos comunes?", ctx)
    assert "65" in build_test_reply("¿Cuántos metros cuadrados?", ctx)
    assert "bodega" in build_test_reply("¿Tiene bodega?", ctx)
    features = build_test_reply("¿Cuáles son sus características?", ctx)
    assert "2 baños" in features and "2 dormitorios" in features
    assert "65" in features and "85000" in features


def test_missing_facts_are_never_invented(isolated):
    db = db_case()
    ctx = load_test_context(db, PHONE)
    reply = build_test_reply("¿Cuántos baños tiene?", ctx)
    assert "No tengo confirmado" in reply
    assert "2 baños" not in reply


def test_short_reply_to_visit_time_preserves_conversation(isolated):
    ctx = {
        "property_code": "6436", "old_price_uf": 2790,
        "new_price_uf": 2690, "property_facts": {"banos": 2},
    }
    hist = [{"customer_text": "oka me interesa",
             "bot_text": "Si quieres conocerlo, dime qué día u horario te acomodaría para una posible visita."}]
    cases = (
        ("el proximo mes", "¿Tienes algún día"),
        ("la próxima semana", "¿Tienes algún día"),
        ("por la tarde", "¿Qué día te acomodaría"),
    )
    for message, expected in cases:
        answer = build_test_reply(
            message, ctx, hist,
            llm=lambda *_: (_ for _ in ()).throw(AssertionError("should not ask model"))
        )
        assert "6436" in answer
        assert expected in answer
        assert "Todavía no hay una visita agendada" in answer



def test_followup_without_visit_prompt_does_not_infer_a_visit(isolated):
    ctx = {"property_code": "6436", "old_price_uf": 2790, "new_price_uf": 2690}
    answer = build_test_reply(
        "el próximo mes", ctx,
        [{"bot_text": "¿Quieres más información del precio?"}],
        llm=lambda *_: "Puedes aclararme a qué te refieres con el próximo mes?",
    )
    assert answer.startswith("Puedes aclararme")


def test_invalid_property_attributes_do_not_leak_into_reply(isolated):
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["caracteristicas"] = {
        "banos": None, "dormitorios": -4, "bodegas": 900, "superficie_total": "inválido",
    }
    ctx = load_test_context(db, PHONE)
    assert ctx["property_facts"] == {}
    assert "No tengo confirmado" in build_test_reply("¿Cuántos baños?", ctx)


def test_trusted_facts_flow_through_isolated_inbound_not_crm(isolated):
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["caracteristicas"] = {"banos": 2}
    sender, calls = sender_spy()
    result = process_test_inbound(db, PHONE, "question-bathrooms", "¿Cuántos baños tiene?", sender=sender)
    assert result["status"] == "test_reply_sent"
    assert "2 baños" in calls[0][1]
    assert len(db["crm_price_drop_pilot_test_turns_v1"].docs) == 1



# Realistic conversations / hostile inputs (all simulated: no real provider).
def _full_pilot_db():
    db = db_case()
    p = db["universo_cartera_prop360"].docs[0]
    p["caracteristicas"] = {
        "banos": 2, "dormitorios": 2, "bodegas": 1,
        "superficie_total": 65, "superficie_util": 60,
        "superficie_terraza": 5, "piso": 6, "ascensores": 2,
        "ano_construccion": 2002, "orientacion": "Sur-Poniente",
        "estacionamientos": None,
        "features": ["piscina", "has_terrace", "mascotas"],
    }
    p["tipo_operacion"]["gastos_comunes"] = 85000
    return db


@pytest.mark.parametrize("message,expected", [
    ("hola", "6436"),
    ("Sí, me interesa", "6436"),
    ("sí", "6436"),
    ("oka me interesa", "6436"),
    ("okey me interesa", "6436"),
    ("me interesa", "6436"),
    ("¿Cuánto cuesta?", "2.690"),
    ("¿Bajó a UF 2690?", "2.690"),
    ("¿Cuántos baños tiene?", "2 baños"),
    ("¿Tiene 2 baños?", "2 baños"),
    ("¿Cuántos dormitorios?", "2 dormitorios"),
    ("¿Cuántos baños y dormitorios?", "2 dormitorios"),
    ("¿Qué superficie tiene?", "65 m² totales"),
    ("¿Cuántos metros cuadrados?", "65 m² totales"),
    ("¿Tiene terraza?", "5 m² de terraza"),
    ("¿En qué piso está?", "piso 6"),
    ("¿Cuál es la orientación?", "Sur-Poniente"),
    ("¿Cuáles son los gastos comunes?", "85.000"),
    ("¿Tiene bodega?", "1 bodega"),
    ("¿Tiene piscina?", "piscina"),
    ("¿Aceptan mascotas?", "reglamento"),
    ("¿Tiene estacionamiento?", "No tengo confirmado"),
    ("¿Cuánto cuesta y cuántos baños tiene?", "2.690"),
    ("¿Cuáles son sus características?", "2 baños"),
    ("Quiero visitarla", "¿Qué día"),
    ("¿Puedo visitar el próximo mes?", "¿Tienes algún día"),
    ("Quiero hacer una oferta", "oferta"),
    ("¿Aceptan ofertas?", "oferta"),
    ("¿Se puede negociar?", "oferta"),
    ("¿Cuánto es la comisión?", "No tengo confirmados"),
    ("Quiero hablar con un ejecutivo", "no se enviará"),
    ("¿Me puede llamar una ejecutiva?", "no se enviará"),
    ("No me interesa", "No insistiremos"),
    ("No quiero visitar", "no coordinaremos"),
    ("¿Tienen otra en Ñuñoa?", "otras"),
    ("Quiero información del código 6500", "6436"),
    ("6500", "6436"),
    ("https://www.procasa.cl/6500", "6436"),
])
def test_simulate_37_customer_questions_without_real_sends(isolated, message, expected):
    db = _full_pilot_db()
    sender, calls = sender_spy()
    def fake_model(q, context, history):
        assert context["property_code"] == "6436"
        return f"Hola, te ayudo con el departamento 6436. ¿Qué deseas conocer?"
    result = process_test_inbound(
        db, PHONE, "sim:" + message, message, sender=sender, llm=fake_model
    )
    assert result == {"status": "test_reply_sent"}
    assert len(calls) == 1
    assert expected.casefold() in calls[0][1].casefold()
    assert set(db) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1"
    }


def test_simulate_full_multi_turn_conversion_without_crm(isolated):
    db = _full_pilot_db()
    sender, calls = sender_spy()
    transcript = [
        ("hola", "6436"),
        ("cuales son sus caracteristicas?", "2 baños"),
        ("pero quiero saber cuantos baños tiene", "2 baños"),
        ("¿cuánto cuesta ahora?", "2.690"),
        ("oka me interesa", "¿Te gustaría saber algo en particular o ir a verlo?"),
        ("quiero visitarla", "¿Qué día"),
        ("el proximo mes", "¿Tienes algún día"),
        ("el martes", "¿Te acomoda más en la mañana"),
        ("por la tarde", "el martes por la tarde"),
        ("¿aceptan ofertas?", "oferta"),
    ]
    for i, (message, fragment) in enumerate(transcript):
        result = process_test_inbound(
            db, PHONE, "transcript:" + str(i), message, sender=sender,
            llm=lambda q, c, h: "¡Hola! ¿Qué más quieres saber del departamento 6436?"
        )
        assert result == {"status": "test_reply_sent"}
        assert fragment.casefold() in calls[-1][1].casefold()
    assert len(calls) == len(transcript)
    turns = db["crm_price_drop_pilot_test_turns_v1"].docs
    assert len(turns) == len(transcript)
    assert all(t["status"] == "SENT" for t in turns)
    assert all(t["campaign_id"] == "test:6436" for t in turns)


def test_simulate_opt_out_does_not_send_or_fall_into_crm(isolated):
    db = _full_pilot_db()
    sender, calls = sender_spy()
    assert process_test_inbound(db, PHONE, "optout", "No me escriban más", sender=sender) == {
        "status": "test_stopped"
    }
    assert not calls
    assert process_test_inbound(db, PHONE, "afteroptout", "sí", sender=sender) == {
        "status": "test_not_active"
    }
    assert not calls


@pytest.mark.parametrize("bad_reply", [
    "He notificado al ejecutivo y te llamará.",
    "Lo dejamos anotado para el martes.",
    "La visita está confirmada.",
    "Te contactaremos mañana.",
    "Queda reservada la visita.",
    "Queda registrado solo como preferencia tuya.",
])
def test_simulated_llm_fabricated_commercial_actions_are_blocked(isolated, bad_reply):
    ctx = load_test_context(_full_pilot_db(), PHONE)
    answer = build_test_reply("¿Tiene conserjería?", ctx,
                              llm=lambda *_: bad_reply)
    assert "Esta es una prueba interna" in answer
    assert "no notificaremos" in answer


def test_simulation_response_fails_closed_on_stale_price_and_unavailable_sale(isolated):
    for prop in (db_case(price=2800), db_case(active=False)):
        sender, calls = sender_spy()
        result = process_test_inbound(prop, PHONE, "wrong-sale", "¿Cuántos baños?", sender=sender)
        assert result["status"] == "test_not_active"
        assert calls == []


def test_same_provider_message_id_is_never_answered_twice(isolated):
    db = _full_pilot_db()
    sender, calls = sender_spy()
    assert process_test_inbound(db, PHONE, "same", "Quiero visitarla", sender=sender)["status"] == "test_reply_sent"
    assert process_test_inbound(db, PHONE, "same", "Quiero visitarla", sender=sender)["status"] == "duplicate_suppressed"
    assert len(calls) == 1


def test_prior_message_context_does_not_assert_a_visit_without_clear_question(isolated):
    ctx = load_test_context(_full_pilot_db(), PHONE)
    answer = build_test_reply(
        "el próximo mes", ctx,
        history=[{"bot_text": "¿Quieres más información o prefieres coordinar una visita?"}],
        llm=lambda *_: "¿Te refieres a recibir información o quieres visitar el próximo mes?",
    )
    assert "¿Te refieres" in answer
    assert "agend" not in answer.lower()



def test_real_november_2_visit_conversation_remembers_day_and_time(isolated):
    """Reproduce the actual failed seven-message test without any real sends."""
    db = _full_pilot_db()
    sender, sent = sender_spy()
    transcript = [
        ("hola", "¡Hola!"),
        ("cuales son sus gastos comunes", "$85.000"),
        ("cuando se puede ir a visitar la propiedad?", "¿Qué día"),
        ("prroximo viernes", "el próximo viernes"),
        ("en la mañana", "el próximo viernes por la mañana"),
        ("en viernes en la mañana", "el próximo viernes por la mañana"),
        ("ya te respondi", "el próximo viernes por la mañana"),
    ]
    for i, (message, expected) in enumerate(transcript):
        result = process_test_inbound(
            db, PHONE, "real-nov-2:" + str(i), message, sender=sender,
            llm=lambda *_: (_ for _ in ()).throw(AssertionError("LLM unnecessary for these intents"))
        )
        assert result["status"] == "test_reply_sent"
        assert expected in sent[-1][1]
    assert len(sent) == len(transcript)
    # No repeated request for an already-provided weekday or daypart.
    for _, reply in sent[4:]:
        assert "¿Qué día" not in reply
        assert "¿Qué horario" not in reply
        assert "registrado" not in reply.casefold()
        assert "agendada ni se notificó" in reply
    assert set(db) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1"
    }
    assert all(r["status"] == "SENT" for r in db["crm_price_drop_pilot_test_turns_v1"].docs)


def test_never_infer_visit_from_price_or_property_reply(isolated):
    ctx = load_test_context(_full_pilot_db(), PHONE)
    history = [
        {"customer_text": "quiero visitarla", "bot_text": "¿Qué día u horario te acomoda para la visita?"},
        {"customer_text": "¿Cuántos baños tiene?", "bot_text": "Según la ficha: 2 baños."},
    ]
    output = build_test_reply(
        "en la mañana", ctx, history, llm=lambda *_: "¿Te refieres a visitar el departamento?"
    )
    assert output == "¿Te refieres a visitar el departamento?"


def test_visit_preference_alone_is_never_a_booking(isolated):
    ctx = load_test_context(_full_pilot_db(), PHONE)
    previous = [{"customer_text": "Quiero visitarla", "bot_text": "¿Qué día u horario te acomoda para la visita?"}]
    assert "¿Te acomoda más en la mañana" in build_test_reply("prroximo viernes", ctx, previous)
    confirmed = build_test_reply(
        "en la mañana", ctx, previous + [
            {"customer_text": "prroximo viernes",
             "bot_text": "¿Te acomoda más en la mañana o en la tarde? Todavía no hay una visita agendada."}
        ],
    )
    assert "el próximo viernes por la mañana" in confirmed
    assert "Todavía no hay una visita agendada" in confirmed
    assert "confirmada" not in confirmed
    assert "notificamos" not in confirmed


def test_freeform_model_must_not_claim_it_saved_customer_preference(isolated):
    ctx = load_test_context(_full_pilot_db(), PHONE)
    reply = build_test_reply(
        "¿Hay conserjería?", ctx, llm=lambda *_: "Queda registrado para el viernes."
    )
    assert "Queda registrado" not in reply
    assert "no notificaremos" in reply



def test_v5_personal_followup_is_explicitly_simulated_on_allowlisted_phone():
    from scripts import price_drop_pilot_test as sender
    from chatbot.price_drop_pilot_copy import build_price_drop_followup
    assert sender.TEST_ID == "20261102-6436-price-drop-pilot-v5"
    source = Path(__file__).resolve().parents[1].joinpath(
        "scripts", "price_drop_pilot_test.py"
    ).read_text()
    assert "prior_inquiry_verified=True" in source
    assert "SIMULATED" in source
    assert '"test_mode": True' in source
    assert '"campaign_version": "price_drop_pilot_v5"' in source
    assert 'if not is_test_phone(phone)' in source
    assert "leads" not in source.split("    # A REAL campaign")[1].split("    if not send:")[0].replace("no lead", "")
    db = _full_pilot_db()
    prop = db["universo_cartera_prop360"].docs[0]
    message = build_price_drop_followup(
        code="6436", commune="San Miguel", old_uf=2790, new_uf=2690,
        property_doc=prop, prior_inquiry_verified=True,
    )
    for expected in (
        "¡Hola! ¿Cómo estás?", "PROCASA", "hace un tiempo nos consultaste",
        "San Miguel", "UF 2.790", "UF 2.690", "100 UF menos",
        "2 dormitorios", "2 baños", "terraza",
        "https://www.procasa.cl/6436", "¿Todavía estás buscando departamento?",
    ):
        assert expected in message
    for prohibited in (
        "PRUEBA INTERNA", "me comentaste", "dijiste que era caro",
        "la otra vez conversamos", "[Nombre]", "Ya agendé", "Se notificará",
    ):
        assert prohibited not in message


def test_v5_does_not_invent_prior_inquiry_or_unverified_features():
    from chatbot.price_drop_pilot_copy import build_price_drop_followup
    result = build_price_drop_followup(
        code="6436", commune="San Miguel", old_uf=2790, new_uf=2690,
        property_doc={"caracteristicas": {"dormitorios": 2, "banos": None}},
        prior_inquiry_verified=False,
    )
    assert "nos consultaste" not in result
    assert "para contarte una novedad" in result
    assert "2 baños" not in result
    assert "terraza" not in result
    assert "100 UF menos" in result


@pytest.mark.parametrize("old,new", [
    (2690, 2690), (2500, 2690), (2790, 1999), (2790, 5001),
])
def test_v5_rejects_ineligible_or_inconsistent_price(old, new):
    from chatbot.price_drop_pilot_copy import build_price_drop_followup
    with pytest.raises(ValueError):
        build_price_drop_followup(
            code="6436", commune="San Miguel", old_uf=old, new_uf=new,
            property_doc={}, prior_inquiry_verified=True,
        )


def test_v3_sender_dry_run_never_calls_provider_or_creates_campaign(isolated, monkeypatch):
    from scripts import price_drop_pilot_test as sender
    db = _full_pilot_db()
    db["crm_price_drop_pilot_v1"].docs = []
    monkeypatch.setattr(sender, "get_db", lambda: db)
    def forbidden_send(*args, **kwargs):
        raise AssertionError("Dry run sent a WhatsApp")
    monkeypatch.setattr(sender, "send_whatsapp_message_detailed_sync", forbidden_send)
    assert sender.run_test_once(send=False) == 0
    assert not db["crm_price_drop_pilot_v1"].docs



def test_live_6436_location_and_quinchos_are_resolved_from_prop360_only(isolated):
    """Reproduce Nov 2's missed factual answers without a model or CRM writes."""
    db = _full_pilot_db()
    prop = db["universo_cartera_prop360"].docs[0]
    prop["ubicacion"] = {
        "comuna": "San Miguel",
        "calle": "Séptima Avenida 1370 68",
        "direccion_referencial": "Séptima Avenida - Tercera Transversal",
    }
    prop["caracteristicas"]["features"].append("has_grill")
    context = load_test_context(db, PHONE)
    assert context["property_facts"]["calle_publicada"] == "Séptima Avenida 1370 68"
    assert context["property_facts"]["parrillas_ficha"] is True
    send, calls = sender_spy()
    conversation = [
        ("Podria ser pero donde quedaba", "Séptima Avenida 1370 68"),
        ("Tiene terraza?", "5 m² de terraza"),
        ("Y quinchos", "parrillas mencionadas en la ficha"),
        ("Tienes la direccion exacta", "Séptima Avenida 1370 68"),
        ("¿Cuántos baños tiene y cuál es la dirección?", "2 baños"),
    ]
    for i, (question, expected) in enumerate(conversation):
        answer = process_test_inbound(
            db, PHONE, "live-facts-" + str(i), question, sender=send,
            llm=lambda *_: (_ for _ in ()).throw(AssertionError(
                "Confirmed property facts must be deterministic"
            )),
        )
        assert answer["status"] == "test_reply_sent"
        assert expected.casefold() in calls[-1][1].casefold()
        assert "no la tengo" not in calls[-1][1].casefold()
    assert "Séptima Avenida 1370 68" in calls[-1][1]
    assert "por confirmar" in calls[2][1]
    assert "quinchos" in calls[2][1]
    assert len(calls) == len(conversation)
    assert set(db) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1"
    }


def test_unverified_quinchos_and_address_are_not_fabricated(isolated):
    ctx = load_test_context(_full_pilot_db(), PHONE)
    assert "calle_publicada" not in ctx["property_facts"]
    assert "parrillas_ficha" not in ctx["property_facts"]
    answer = build_test_reply(
        "¿Tiene quinchos?", ctx,
        llm=lambda *_: (_ for _ in ()).throw(AssertionError("No model")),
    )
    assert "no hay confirmación de quinchos" in answer
    address = build_test_reply(
        "¿Dónde queda?", ctx,
        llm=lambda *_: (_ for _ in ()).throw(AssertionError("No model")),
    )
    assert "San Miguel" in address and "dirección exacta no confirmada" in address
    assert "Séptima Avenida" not in address



def test_live_november_2_mnana_two_time_options_and_clarification(isolated):
    """Reproduce the real failure: typo, 9 or 10, repeated day and 'Que? Siii'."""
    db = _full_pilot_db()
    sender, sent = sender_spy()
    exchange = [
        ("Ok cuandonse puede ir a ver", "¿Qué día"),
        ("Mñana tipo 9 o 10", "mañana a las 9 o 10"),
        ("Mñana te dije", "mañana a las 9 o 10"),
        ("Mañana por tercera vez ye dije", "mañana a las 9 o 10"),
        ("Que?", "mañana a las 9 o 10"),
        ("Siii", "mañana a las 9 o 10"),
        ("Me contactarán?", "no se notifica a ningún ejecutivo"),
    ]
    for i, (msg, expected) in enumerate(exchange):
        result = process_test_inbound(
            db, PHONE, "visit-nov2-" + str(i), msg,
            sender=sender,
            llm=lambda *_: (_ for _ in ()).throw(AssertionError(
                "Clear visit thread or contact question should not invoke a model"
            )),
        )
        assert result["status"] == "test_reply_sent"
        assert expected.casefold() in sent[-1][1].casefold()
    for _, answer in sent[1:6]:
        assert "¿Qué día" not in answer
        assert "registrad" not in answer.casefold()
        assert "agendada" in answer
        assert "a las 9 o 10" in answer
    assert len(sent) == len(exchange)
    assert all(t["status"] == "SENT" for t in db["crm_price_drop_pilot_test_turns_v1"].docs)


def test_llm_cannot_claim_that_visit_preference_was_registered(isolated):
    ctx = load_test_context(_full_pilot_db(), PHONE)
    answer = build_test_reply(
        "¿Hay conserjería?", ctx,
        llm=lambda *_: "Queda solo registrada tu preferencia."
    )
    assert "Queda solo registrada" not in answer
    assert "no notificaremos" in answer


def test_morning_daypart_does_not_erase_an_explicit_tomorrow(isolated):
    ctx = load_test_context(_full_pilot_db(), PHONE)
    history = [{"customer_text": "Quiero visitar la propiedad",
                "bot_text": "¿Qué día u horario te acomoda para la visita?"}]
    reply = build_test_reply(
        "mañana por la mañana", ctx, history,
        llm=lambda *_: (_ for _ in ()).throw(AssertionError("No model"))
    )
    assert "mañana por la mañana" in reply


def test_intermediate_unrelated_property_question_ends_visit_thread(isolated):
    ctx = load_test_context(_full_pilot_db(), PHONE)
    history = [
        {"customer_text": "Quiero visitarla", "bot_text": "¿Qué día te acomoda para la visita?"},
        {"customer_text": "¿Cuántos dormitorios?", "bot_text": "Tiene 2 dormitorios."},
    ]
    assert build_test_reply(
        "Qué?", ctx, history, llm=lambda *_: "¿Quieres que lo aclare?"
    ) == "¿Quieres que lo aclare?"



def test_v4_responds_in_short_natural_whatsapp_without_reintroducing_ourselves(isolated):
    ctx = load_test_context(_full_pilot_db(), PHONE)
    first = build_test_reply("Hola", ctx, [])
    followup = build_test_reply("hola", ctx, [
        {"customer_text": "Hola", "bot_text": first},
        {"customer_text": "Tiene terraza?", "bot_text": "Sí, tiene 5 m² de terraza."},
    ])
    assert "bajó" in first and "2.690" in first
    assert "6436" in first and "6436" in followup
    assert "Soy de PROCASA" not in followup
    assert "¡Hola de nuevo!" in followup
    assert first.count("¿") == 1


def test_v4_direct_property_answer_not_long_form_or_repetitive(isolated):
    db = _full_pilot_db()
    ctx = load_test_context(db, PHONE)
    reply = build_test_reply("¿Tiene terraza?", ctx)
    assert reply.startswith("Sí, ")
    assert "5 m² de terraza" in reply
    assert "Según la ficha vigente del departamento" not in reply
    assert "UF 2.690" not in reply
    assert len(reply) < 160


def test_v4_guarded_llm_uses_same_commercial_style_and_price_drop_context(monkeypatch, isolated):
    import sys
    from types import SimpleNamespace
    from chatbot import price_drop_pilot_isolation as pilot
    seen = []
    def fake_create(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="¿Te gustaría que te cuente un poco más?"),
            finish_reason="stop"
        )])
    fake_client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=fake_create)))
    monkeypatch.setitem(sys.modules, "chatbot.grok_client", SimpleNamespace(client=fake_client))
    ctx = load_test_context(_full_pilot_db(), PHONE)
    history = [{"customer_text": "Me interesa", "bot_text": "¡Qué bueno!"}]
    output = pilot._llm_answer("¿Cómo es el sector?", ctx, history)
    assert output == "¿Te gustaría que te cuente un poco más?"
    sys = seen[0]["messages"][0]["content"]
    assert "asistente comercial habitual" in sys
    assert "rebaja de precio" in sys
    assert "2.790" in sys and "2.690" in sys
    assert "no repitas saludos" in sys
    assert "NO se escriben leads ni CRM" in sys
    assert seen[0]["messages"][-2]["role"] == "assistant"
    assert seen[0]["messages"][-1]["content"] == "¿Cómo es el sector?"


def test_v4_llm_cannot_send_truncated_sentence(monkeypatch, isolated):
    import sys
    from types import SimpleNamespace
    from chatbot import price_drop_pilot_isolation as pilot
    def clipped(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="Sí, te recomiendo ver"), finish_reason="length"
        )])
    fake_client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=clipped)))
    monkeypatch.setitem(sys.modules, "chatbot.grok_client", SimpleNamespace(client=fake_client))
    ctx = load_test_context(_full_pilot_db(), PHONE)
    answer = build_test_reply("¿Tiene conserjería?", ctx)
    assert "Sí, te recomiendo ver" not in answer
    assert "no notificaremos" in answer
