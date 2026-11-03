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
    assert "¡Qué bueno!" in calls[0][1]
    assert db["crm_price_drop_pilot_test_turns_v1"].docs[0]["status"] == "SENT"
    assert set(db.keys()) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1"
    }


@pytest.mark.parametrize("message,fragment", [
    ("Sí, me interesa", "¡Qué bueno!"),
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
    assert "no se ha solicitado" in answer.lower()


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
    ("hola", "Gracias por responder"),
    ("Sí, me interesa", "¡Qué bueno!"),
    ("sí", "¡Qué bueno!"),
    ("oka me interesa", "¡Qué bueno!"),
    ("okey me interesa", "¡Qué bueno!"),
    ("me interesa", "¡Qué bueno!"),
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
        ("hola", "Gracias por responder"),
        ("cuales son sus caracteristicas?", "2 baños"),
        ("pero quiero saber cuantos baños tiene", "2 baños"),
        ("¿cuánto cuesta ahora?", "2.690"),
        ("oka me interesa", "podemos revisar tus dudas"),
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
    assert "no se ha solicitado ninguna gestión" in answer
    assert "no se ha solicitado" in answer


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
    assert "no se ha solicitado" in reply



def test_v6_sender_fresh_id_and_copy_is_warm_specific_and_historically_grounded():
    from scripts import price_drop_pilot_test as sender
    assert sender.TEST_ID == "20261103-6436-price-drop-pilot-v6-fresh-dialog"
    source = Path(__file__).resolve().parents[1].joinpath(
        "scripts", "price_drop_pilot_test.py"
    ).read_text()
    message_source = source.split("    message = (", 1)[1].split("    if not send:", 1)[0]
    for expected in (
        "Hola, ¿cómo estás? Soy de PROCASA", "San Miguel",
        "UF 2.790", "UF 2.690", "100 UF menos",
        "https://www.procasa.cl/6436",
        "¿Cómo lo ves con este nuevo valor?",
    ):
        assert expected in message_source
    for prohibited in (
        "PRUEBA INTERNA", "habías consultado", "me comentaste",
        "te acordarás", "la otra vez conversamos", "[Nombre]",
        "Se notificará", "le avisaremos al ejecutivo",
        "te interesó la última vez", "te habíamos contactado",
    ):
        assert prohibited not in message_source
    assert '"test_mode": True' in source
    assert '"campaign_version": "price_drop_pilot_v6"' in source
    assert 'if not is_test_phone(phone)' in source


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
    assert "no se ha solicitado" in answer


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
    assert "Gracias por responder" in first
    assert "San Miguel" in first
    assert "2.690" not in first and "6436" not in first
    assert "¿Qué te gustaría saber?" not in first
    assert "Soy de PROCASA" not in followup
    assert followup == "¡Hola de nuevo! 😊 Te leo."
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
    assert "no se ha solicitado" in answer


def test_v5_customer_introduction_is_remembered_without_touching_crm(isolated):
    db = _full_pilot_db()
    send, calls = sender_spy()
    assert process_test_inbound(
        db, PHONE, "name-1", "Hola, soy Camila", sender=send
    )["status"] == "test_reply_sent"
    assert "Camila" in calls[-1][1]
    campaign = db["crm_price_drop_pilot_v1"].docs[0]
    assert campaign["customer_first_name"] == "Camila"
    assert campaign["customer_first_name_source"] == "customer_message"
    # Earlier customer name leaves the 12-turn history window, but persists
    # ONLY in the isolated campaign and can still be recalled reliably.
    for i in range(15):
        assert process_test_inbound(
            db, PHONE, "name-filler-" + str(i), "¿Tiene terraza?", sender=send,
        )["status"] == "test_reply_sent"
    assert process_test_inbound(
        db, PHONE, "name-recall", "¿Cómo me llamo?", sender=send,
    )["status"] == "test_reply_sent"
    assert "Camila" in calls[-1][1]
    assert set(db) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1",
    }


def test_v5_name_in_combined_introduction_does_not_swallow_property_question(isolated):
    db = _full_pilot_db()
    send, calls = sender_spy()
    outcome = process_test_inbound(
        db, PHONE, "name-and-baths",
        "Hola, soy María José, ¿cuántos baños tiene?", sender=send,
        llm=lambda *_: (_ for _ in ()).throw(AssertionError(
            "Confirmed property facts must remain deterministic"
        )),
    )
    assert outcome["status"] == "test_reply_sent"
    assert "María José" in calls[-1][1]
    assert "2 baños" in calls[-1][1]
    assert db["crm_price_drop_pilot_v1"].docs[0]["customer_first_name"] == "María José"


def test_v5_uses_name_sparingly_and_accepts_explicit_correction(isolated):
    db = _full_pilot_db()
    send, calls = sender_spy()
    for key, msg in [
        ("n-1", "Me llamo Juan Pablo"),
        ("n-2", "¿Tiene terraza?"),
        ("n-3", "¿Cuántos baños tiene?"),
        ("n-4", "Quiero visitarla"),
    ]:
        assert process_test_inbound(db, PHONE, key, msg, sender=send)["status"] == "test_reply_sent"
    assert calls[-1][1].count("Juan Pablo") <= 1
    assert "Juan Pablo" in calls[-1][1]
    assert process_test_inbound(
        db, PHONE, "n-5", "No, me llamo Andrés", sender=send
    )["status"] == "test_reply_sent"
    assert db["crm_price_drop_pilot_v1"].docs[0]["customer_first_name"] == "Andrés"
    assert process_test_inbound(
        db, PHONE, "n-6", "¿Recuerdas mi nombre?", sender=send
    )["status"] == "test_reply_sent"
    assert "Andrés" in calls[-1][1]
    assert "Juan Pablo" not in calls[-1][1]


def test_v5_never_assumes_someone_elses_name_or_occupation(isolated):
    db = _full_pilot_db()
    send, calls = sender_spy()
    for key, msg in [
        ("other-name", "Mi esposa se llama Patricia"),
        ("non-name", "Soy interesado"),
        ("wrong-name", "Hola, soy de PROCASA"),
    ]:
        assert process_test_inbound(
            db, PHONE, key, msg, sender=send,
            llm=lambda *_: "Gracias por escribir.",
        )["status"] == "test_reply_sent"
    campaign = db["crm_price_drop_pilot_v1"].docs[0]
    assert "customer_first_name" not in campaign
    assert process_test_inbound(
        db, PHONE, "unknown-name", "¿Cuál es mi nombre?", sender=send
    )["status"] == "test_reply_sent"
    assert "no me has indicado tu nombre" in calls[-1][1]


def test_v5_duplicate_name_event_does_not_update_campaign_again(isolated):
    db = _full_pilot_db()
    send, calls = sender_spy()
    assert process_test_inbound(
        db, PHONE, "intro-duplicate", "Soy Daniela", sender=send
    )["status"] == "test_reply_sent"
    first_updates = sum(op == "update_one" for op, _ in db["crm_price_drop_pilot_v1"].calls)
    assert process_test_inbound(
        db, PHONE, "intro-duplicate", "Soy Valentina", sender=send
    ) == {"status": "duplicate_suppressed"}
    assert db["crm_price_drop_pilot_v1"].docs[0]["customer_first_name"] == "Daniela"
    assert sum(op == "update_one" for op, _ in db["crm_price_drop_pilot_v1"].calls) == first_updates
    assert len(calls) == 1


def test_live_november_3_outbound_whatsapp_exchange_is_contextual_not_generic(isolated):
    """Replay BOTH customer messages from the Nov 3 V5 WhatsApp pilot."""
    db = _full_pilot_db()
    send, sent = sender_spy()
    for i, message in enumerate([
        "HOLA",
        "COMO QYE ME GUSTARIA SABER SI TU ME ESTAS ESCROBIENDO",
    ]):
        result = process_test_inbound(
            db, PHONE, "nov3-real-" + str(i), message, sender=send,
            llm=lambda *_: (_ for _ in ()).throw(
                AssertionError("These two reactions must be deterministic")
            ),
        )
        assert result == {"status": "test_reply_sent"}
    greeting, correction = sent[0][1], sent[1][1]
    assert "Gracias por responder" in greeting
    assert "te escribí" in greeting.casefold()
    assert "UF 2.690" not in greeting
    assert "¿Qué te gustaría saber?" not in greeting
    assert "fuimos nosotros quienes te escribimos" in correction
    assert "Disculpa" in correction
    for bad in ("desconfianza", "asistente virtual", "precio anterior",
                "¿Qué te gustaría saber?", "6436"):
        assert bad.casefold() not in correction.casefold()
    assert all(x["status"] == "SENT"
               for x in db["crm_price_drop_pilot_test_turns_v1"].docs)
    assert set(db) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1",
    }


@pytest.mark.parametrize("message", [
    "Pero si ustedes me escribieron primero",
    "¿Y por qué me preguntas qué quiero saber si tú me escribiste?",
    "¿Tú me estás contactando por una rebaja?",
    "¿Cómo que me gustaría saber si tú me estás escribiendo?",
])
def test_outbound_contact_objection_acknowledges_sender_without_fabrication(
    isolated, message
):
    ctx = load_test_context(_full_pilot_db(), PHONE)
    response = build_test_reply(
        message, ctx,
        llm=lambda *_: (_ for _ in ()).throw(AssertionError("No LLM")),
    )
    assert "fuimos nosotros quienes te escribimos" in response
    assert "desconfianza" not in response
    assert "visita agendada" not in response


def test_followup_after_outbound_correction_continues_with_verified_facts(isolated):
    db = _full_pilot_db()
    send, calls = sender_spy()
    for i, message in enumerate([
        "HOLA",
        "COMO QYE ME GUSTARIA SABER SI TU ME ESTAS ESCROBIENDO",
        "¿Tiene terraza?",
        "¿Y sus gastos comunes?",
        "Me llamo Camila",
        "¿Cuántos baños tiene?",
    ]):
        result = process_test_inbound(
            db, PHONE, "nov3-continued-" + str(i), message, sender=send,
            llm=lambda *_: (_ for _ in ()).throw(AssertionError("No LLM")),
        )
        assert result == {"status": "test_reply_sent"}
    assert "5 m² de terraza" in calls[2][1]
    assert "$85.000" in calls[3][1]
    assert "Camila" in calls[4][1]
    assert "2 baños" in calls[5][1]
    assert "Camila" in db["crm_price_drop_pilot_v1"].docs[0]["customer_first_name"]


def test_native_pilot_uses_real_production_commercial_generator_and_outreach_history(
    isolated, monkeypatch
):
    """No second bot: identical structured generator, isolated context only."""
    # The CI environment intentionally has no provider credentials.
    # A dummy SDK key allows import; the generator is monkeypatched below,
    # so these regression tests never call any remote model.
    monkeypatch.setenv("OPENAI_API_KEY", "ci-test-dummy-key-no-network")
    from chatbot import grok_client
    db = _full_pilot_db()
    send, sent = sender_spy()
    seen = []
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "true")

    def native_model(messages, prospect, **kwargs):
        seen.append((messages, prospect, kwargs))
        return {
            "intencion": "consulta_general",
            "respuesta_bot": "¡Hola! Qué bueno que me respondiste. ¿Alcanzaste a mirar la publicación?",
            "datos_extraidos": {"rut": "NOT_SAVED"},
        }

    monkeypatch.setattr(grok_client, "generar_respuesta_estructurada", native_model)
    outcome = process_test_inbound(db, PHONE, "natural-1", "HOLA", sender=send)
    assert outcome == {"status": "test_reply_sent"}
    assert "Qué bueno que me respondiste" in sent[-1][1]
    messages, prospect, kwargs = seen[0]
    assert "Eres una ejecutiva senior de Procasa" in messages[0]["content"]
    assert "PROCASA, no el cliente, inició este contacto" in messages[1]["content"]
    assert "3.58%" in messages[1]["content"]  # 100 UF / 2.790 UF
    assert "UF 2.690" in messages[2]["content"]
    assert messages[2]["role"] == "assistant"
    assert messages[-1] == {"role": "user", "content": "HOLA"}
    assert prospect == {"operacion": "Venta"}
    assert kwargs["write_telemetry"] is False
    assert set(db) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1",
    }


def test_native_pilot_replays_real_whatsapp_dialogue_with_full_context(
    isolated, monkeypatch
):
    # The CI environment intentionally has no provider credentials.
    # A dummy SDK key allows import; the generator is monkeypatched below,
    # so these regression tests never call any remote model.
    monkeypatch.setenv("OPENAI_API_KEY", "ci-test-dummy-key-no-network")
    from chatbot import grok_client
    db = _full_pilot_db()
    send, sent = sender_spy()
    seen = []
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "1")

    def native_model(messages, prospect, **kwargs):
        seen.append(messages)
        last = messages[-1]["content"]
        if last == "HOLA":
            reply = "¡Hola! Gracias por responderme 😊. ¿Alcanzaste a mirar el departamento?"
        elif last == "COMO QYE ME GUSTARIA SABER SI TU ME ESTAS ESCROBIENDO":
            reply = ("Tienes razón, fui yo quien te escribió para contarte de la rebaja. "
                     "Disculpa que te preguntara como si tú hubieras iniciado el contacto.")
        elif last == "¿Cuántos baños tiene?":
            reply = "Tiene 2 baños. ¿Quieres que te cuente algo más sobre la distribución?"
        else:
            raise AssertionError("Unexpected user message: " + last)
        return {"intencion": "consulta_general", "respuesta_bot": reply,
                "datos_extraidos": {}}

    monkeypatch.setattr(grok_client, "generar_respuesta_estructurada", native_model)
    for i, msg in enumerate([
        "HOLA",
        "COMO QYE ME GUSTARIA SABER SI TU ME ESTAS ESCROBIENDO",
        "¿Cuántos baños tiene?",
    ]):
        assert process_test_inbound(
            db, PHONE, "natural-replay-" + str(i), msg, sender=send,
        )["status"] == "test_reply_sent"
    assert len(seen) == 3
    last_messages = seen[-1]
    assert [m["content"] for m in last_messages if m["role"] == "user"] == [
        "HOLA",
        "COMO QYE ME GUSTARIA SABER SI TU ME ESTAS ESCROBIENDO",
        "¿Cuántos baños tiene?",
    ]
    assert "Tienes razón" in last_messages[-2]["content"]
    assert "2 baños" in sent[-1][1]
    assert all(t["status"] == "SENT"
               for t in db["crm_price_drop_pilot_test_turns_v1"].docs)


def test_native_pilot_remembers_name_without_reading_live_leads(
    isolated, monkeypatch
):
    # The CI environment intentionally has no provider credentials.
    # A dummy SDK key allows import; the generator is monkeypatched below,
    # so these regression tests never call any remote model.
    monkeypatch.setenv("OPENAI_API_KEY", "ci-test-dummy-key-no-network")
    from chatbot import grok_client
    db = _full_pilot_db()
    sender, sent = sender_spy()
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "yes")
    contexts = []

    def native_model(messages, prospect, **kwargs):
        contexts.append(messages[1]["content"])
        return {"intencion": "consulta_general",
                "respuesta_bot": "Gracias por escribirme. Cuéntame qué te interesa del departamento."}

    monkeypatch.setattr(grok_client, "generar_respuesta_estructurada", native_model)
    for i, msg in enumerate(["Hola, soy María José", "¿Tiene terraza?"]):
        assert process_test_inbound(
            db, PHONE, "natural-name-" + str(i), msg, sender=sender,
        )["status"] == "test_reply_sent"
    assert "Nombre confirmado por el propio cliente: María José" in contexts[-1]
    assert db["crm_price_drop_pilot_v1"].docs[0]["customer_first_name"] == "María José"
    assert "leads" in db and isinstance(db["leads"], BlockedCollection)


def test_native_pilot_blocks_unverified_operations_and_sensitive_requests(
    isolated, monkeypatch
):
    # The CI environment intentionally has no provider credentials.
    # A dummy SDK key allows import; the generator is monkeypatched below,
    # so these regression tests never call any remote model.
    monkeypatch.setenv("OPENAI_API_KEY", "ci-test-dummy-key-no-network")
    from chatbot import grok_client
    db = _full_pilot_db()
    sender, sent = sender_spy()
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "true")

    unsafe = iter([
        "Te agendé una visita para mañana.",
        "Envíame tu RUT para poder ayudarte.",
        "Un ejecutivo te llamará mañana.",
    ])
    monkeypatch.setattr(
        grok_client, "generar_respuesta_estructurada",
        lambda messages, prospect, **kwargs: {
            "intencion": "agendar_visita", "respuesta_bot": next(unsafe)
        },
    )
    for i, query in enumerate(["Me interesa", "¿Qué piden?", "¿Me llamarán?"]):
        assert process_test_inbound(
            db, PHONE, "natural-unsafe-" + str(i), query, sender=sender,
        )["status"] == "test_reply_sent"
        assert "no se ha solicitado ninguna gestión" in sent[-1][1]
    assert set(db) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1",
    }


def test_native_pilot_is_opt_in_and_unrelated_phones_never_invoke_generator(
    isolated, monkeypatch
):
    # The CI environment intentionally has no provider credentials.
    # A dummy SDK key allows import; the generator is monkeypatched below,
    # so these regression tests never call any remote model.
    monkeypatch.setenv("OPENAI_API_KEY", "ci-test-dummy-key-no-network")
    from chatbot import grok_client
    db = _full_pilot_db()
    sender, sent = sender_spy()
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "1")
    monkeypatch.setattr(
        grok_client, "generar_respuesta_estructurada",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Unauthorized phone invoked production generator")
        ),
    )
    assert process_test_inbound(
        db, "+56988889999", "out", "Hola", sender=sender
    ) == {"status": "not_test_phone"}
    assert not sent
    monkeypatch.delenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION")
    assert process_test_inbound(
        db, PHONE, "legacy-1", "Hola", sender=sender,
        llm=lambda *_: "No additional model needed",
    )["status"] == "test_reply_sent"
    assert "Gracias por responder" in sent[-1][1]


def _seed_long_pilot_history(db, count=85):
    """Offline historical exchange fixture; no lead/CRM access."""
    now = datetime.now(timezone.utc) - timedelta(minutes=20)
    campaign_id = db["crm_price_drop_pilot_v1"].docs[0]["_id"]
    for i in range(count):
        if i == 2:
            customer = "Mi prioridad es una terraza y un presupuesto máximo de UF 2700."
            bot = "Entiendo: priorizas la terraza y un presupuesto de UF 2700."
        elif i == 4:
            customer = "Quiero ir a visitar el departamento."
            bot = "¿Qué día te acomodaría?"
        elif i == 5:
            customer = "Prefiero el viernes a las 10."
            bot = "Perfecto, tengo presente tu preferencia. No hay reserva confirmada."
        else:
            customer = f"Consulta histórica {i}: seguimiento de la conversación."
            bot = f"Respuesta previa {i}: continuamos con los antecedentes."
        db["crm_price_drop_pilot_test_turns_v1"].docs.append({
            "_id": f"old-turn-{i}",
            "campaign_id": campaign_id, "status": "SENT",
            "customer_text": customer, "bot_text": bot,
            "created_at": now + timedelta(seconds=i),
        })


def test_extended_campaign_context_includes_early_customer_facts_and_bot_answers(
    isolated, monkeypatch
):
    # The real generator is used, but fake credentials/model prevent API calls.
    monkeypatch.setenv("OPENAI_API_KEY", "ci-test-only")
    from chatbot import grok_client
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "1")
    db = _full_pilot_db()
    _seed_long_pilot_history(db, count=85)
    seen = []
    def model(messages, prospect, **kwargs):
        seen.append(messages)
        return {"intencion": "consulta_general", "datos_extraidos": {},
                "respuesta_bot": (
                    "Claro, seguimos conversando. ¿Prefieres que revisemos "
                    "alguna duda pendiente sobre el departamento?"
                )}
    monkeypatch.setattr(grok_client, "generar_respuesta_estructurada", model)
    sender, sent = sender_spy()
    result = process_test_inbound(
        db, PHONE, "long-history-1", "Gracias", sender=sender
    )
    assert result == {"status": "test_reply_sent"}
    content = seen[0]
    archive = next(m["content"] for m in content
                   if "TRANSCRIPCIÓN ANTERIOR" in m["content"])
    assert "Mi prioridad es una terraza" in archive
    assert "presupuesto máximo de UF 2700" in archive
    assert "priorizas la terraza y un presupuesto" in archive
    assert "Prefiero el viernes a las 10" in archive
    # The last 36 exchanges retain actual user/assistant roles and order.
    assert [m["role"] for m in content[-3:]] == ["user", "assistant", "user"]
    assert content[-1] == {"role": "user", "content": "Gracias"}
    assert len(content) > 60
    assert len(sent) == 1
    assert set(db) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1",
    }


def test_extended_history_is_bounded_to_240_sent_exchanges(isolated, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ci-test-only")
    from chatbot import grok_client
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "true")
    db = _full_pilot_db()
    _seed_long_pilot_history(db, count=248)
    seen = []
    def model(messages, prospect, **kwargs):
        seen.append(messages)
        return {"intencion": "consulta_general", "respuesta_bot": (
            "Perfecto, sigo atento a lo que necesites sobre este departamento."
        )}
    monkeypatch.setattr(grok_client, "generar_respuesta_estructurada", model)
    sender, sent = sender_spy()
    assert process_test_inbound(
        db, PHONE, "long-history-240", "De acuerdo", sender=sender
    )["status"] == "test_reply_sent"
    full = " ".join(m["content"] for m in seen[0])
    assert "Consulta histórica 0" not in full
    assert "Consulta histórica 211" in full
    assert "Consulta histórica 247" in full
    assert len(full) < 46000
    assert len(sent) == 1


def test_native_pilot_repairs_near_duplicate_response_before_single_whatsapp_send(
    isolated, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "ci-test-only")
    from chatbot import grok_client
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "true")
    db = _full_pilot_db()
    now = datetime.now(timezone.utc)
    previous = ("Qué bueno que te interese el departamento de San Miguel, "
                "puedes revisar la publicación y me dices qué te parece.")
    db["crm_price_drop_pilot_test_turns_v1"].docs.append({
        "_id": "previous-sent", "campaign_id": "test:6436", "status": "SENT",
        "customer_text": "Me interesa", "bot_text": previous,
        "created_at": now - timedelta(minutes=1),
    })
    calls = []
    def model(messages, prospect, **kwargs):
        calls.append(messages)
        return {
            "intencion": "consulta_general", "datos_extraidos": {},
            "respuesta_bot": (previous if len(calls) == 1 else
                "Perfecto, tengo presente tu interés. Si quieres, podemos "
                "conversar sobre una posible visita, sin compromiso."),
        }
    monkeypatch.setattr(grok_client, "generar_respuesta_estructurada", model)
    sender, sent = sender_spy()
    result = process_test_inbound(db, PHONE, "duplicate-repair-1", "Sí", sender=sender)
    assert result["status"] == "test_reply_sent"
    assert len(calls) == 2
    assert len(sent) == 1
    assert sent[0][1] != previous
    assert "tengo presente tu interés" in sent[0][1]


def test_native_pilot_does_not_reask_name_or_visit_day_already_given(
    isolated, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "ci-test-only")
    from chatbot import grok_client
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "1")
    db = _full_pilot_db()
    campaign = db["crm_price_drop_pilot_v1"].docs[0]
    campaign.update({
        "customer_first_name": "Camila",
        "customer_first_name_source": "customer_message",
    })
    history = [
        ("Quiero visitar el departamento.", "¿Qué día te acomodaría?"),
        ("Prefiero el viernes a las 10.", "Gracias por indicarme tu preferencia."),
    ]
    now = datetime.now(timezone.utc)
    for i, (customer, bot) in enumerate(history):
        db["crm_price_drop_pilot_test_turns_v1"].docs.append({
            "_id": "visit-seed-" + str(i), "campaign_id": "test:6436",
            "status": "SENT", "created_at": now - timedelta(seconds=100-i),
            "customer_text": customer, "bot_text": bot,
        })
    calls = []
    def model(messages, prospect, **kwargs):
        calls.append(messages)
        return {"intencion": "consulta_general", "datos_extraidos": {},
                "respuesta_bot": (
                    "Camila, ¿qué día te acomoda la visita?"
                    if len(calls) == 1 else
                    "Camila, tengo presente que prefieres el viernes a las 10. "
                    "La disponibilidad sigue por confirmar."
                )}
    monkeypatch.setattr(grok_client, "generar_respuesta_estructurada", model)
    sender, sent = sender_spy()
    assert process_test_inbound(
        db, PHONE, "no-repeat-visit", "¿Podemos avanzar?", sender=sender
    )["status"] == "test_reply_sent"
    assert len(calls) == 2
    assert len(sent) == 1
    assert "¿qué día" not in sent[0][1].casefold()
    assert "viernes a las 10" in sent[0][1]


@pytest.mark.parametrize("customer_message", [
    "CUANDO BAJO",
    "CUANDO HA BAJADO LA PROPEIDAD QUE ANTES PREGUNTE",
    "¿Cuándo bajó el departamento?",
    "¿Qué día bajó el precio?",
    "¿Desde qué fecha se rebajó?",
    "Hace cuánto bajó el precio?",
])
def test_v6_actual_price_drop_date_is_grounded_and_no_llm_needed(
    isolated, customer_message
):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["historial_cambios"] = [
        {
            "fecha": "2026-10-06T21:55:59-03:00",
            "campo": "precio_uf",
            "valor_anterior": 2790,
            "valor_nuevo": 2690,
            "fuente": "prop360_bitacora",
        },
        {
            "fecha": "2026-10-31T21:55:59-03:00",
            "campo": "precio_uf",
            "valor_anterior": 2890,
            "valor_nuevo": 2790,
            "fuente": "prop360_bitacora",
        },
    ]
    ctx = load_test_context(db, PHONE)
    assert ctx["price_drop_date"] == "6 de octubre de 2026"
    answer = build_native_pilot_reply(
        customer_message, ctx, [], generator=lambda *_args, **_kw: (
            _ for _ in ()
        ).throw(AssertionError("No LLM should handle an audited date"))
    )
    assert "6 de octubre de 2026" in answer
    assert "te escribimos" in answer.lower()
    assert "no se ha solicitado" not in answer


def test_unverified_discount_date_is_never_invented(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["historial_cambios"] = [
        {"fecha": "2026-10-06T21:55:59-03:00", "campo": "precio_uf",
         "valor_anterior": 2890, "valor_nuevo": 2690,
         "fuente": "prop360_bitacora"},
    ]
    ctx = load_test_context(db, PHONE)
    assert ctx["price_drop_date"] is None
    result = build_native_pilot_reply(
        "CUANDO BAJO", ctx, [],
        generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Must not call model")
        )
    )
    assert "fecha exacta" in result
    assert "6 de octubre" not in result


def test_v6_price_change_date_survives_real_isolated_whatsapp_path(
    isolated, monkeypatch
):
    from chatbot import grok_client
    from chatbot.price_drop_pilot_isolation import (
        _build_native_campaign_messages,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "ci-test-only")
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "true")
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["historial_cambios"] = [
        {"fecha": "2026-10-06T21:55:59-03:00", "campo": "precio_uf",
         "valor_anterior": 2790, "valor_nuevo": 2690,
         "fuente": "prop360_bitacora"},
    ]
    ctx = load_test_context(db, PHONE)
    msg = _build_native_campaign_messages("¿Sigue vigente?", ctx, [])
    assert "6 de octubre de 2026" in msg[1]["content"]
    assert msg[2]["role"] == "assistant"
    assert "Te escribo porque el departamento" in msg[2]["content"]
    sender, calls = sender_spy()
    # No model needed for date question and only ONE outbound provider call.
    assert process_test_inbound(
        db, PHONE, "date-e2e-v6", "CUANDO BAJO",
        sender=sender,
    )["status"] == "test_reply_sent"
    assert len(calls) == 1
    assert "6 de octubre de 2026" in calls[0][1]
    assert "leads" in db and isinstance(db["leads"], BlockedCollection)
    assert len(db["crm_price_drop_pilot_test_turns_v1"].docs) == 1


def test_price_change_date_from_untrusted_source_is_discarded(isolated):
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["historial_cambios"] = [
        {"fecha": "2026-10-06T21:55:59-03:00", "campo": "precio_uf",
         "valor_anterior": 2790, "valor_nuevo": 2690,
         "fuente": "not_prop360"},
    ]
    assert load_test_context(db, PHONE)["price_drop_date"] is None


def test_deepseek_pilot_json_mode_recovers_from_whitespace_http_200(
    isolated, monkeypatch
):
    import json
    from types import SimpleNamespace
    monkeypatch.setenv("OPENAI_API_KEY", "ci-no-network-test")
    from chatbot import grok_client
    calls = []

    def fake_api(**kwargs):
        calls.append(kwargs)
        raw = " " * 96 if len(calls) == 1 else json.dumps({
            "intencion": "consulta_general",
            "respuesta_bot": "Fuimos nosotros quienes te avisamos de la rebaja.",
            "datos_extraidos": {},
        })
        choice = SimpleNamespace(
            message=SimpleNamespace(content=raw),
            finish_reason="stop",
        )
        return SimpleNamespace(choices=[choice], usage=None)

    monkeypatch.setattr(grok_client, "client",
        SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=fake_api))))
    messages = [
        {"role": "system", "content":
            "[CONTEXTO COMERCIAL DEL MENSAJE SALIENTE - PRIORIDAD]"},
        {"role": "assistant", "content":
            "Hola, te escribimos para avisar de la rebaja de UF 2.790 a UF 2.690."},
        {"role": "user", "content": "¿Cuándo bajó?"},
    ]
    result = grok_client.generar_respuesta_estructurada(
        messages, {"operacion": "Venta"}, write_telemetry=False
    )
    assert "nosotros quienes" in result["respuesta_bot"]
    assert len(calls) == 2
    assert all(
        x.get("response_format") == {"type": "json_object"} for x in calls
    )
    assert all(x["messages"][-1]["content"] == "¿Cuándo bajó?" for x in calls)


def test_deepseek_pilot_json_mode_double_invalid_does_not_retry_indefinitely(
    isolated, monkeypatch
):
    from types import SimpleNamespace
    monkeypatch.setenv("OPENAI_API_KEY", "ci-no-network-test")
    from chatbot import grok_client
    calls = []

    def fake_api(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="    "),
            finish_reason="stop"
        )], usage=None)

    monkeypatch.setattr(grok_client, "client",
        SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=fake_api))))
    result = grok_client.generar_respuesta_estructurada([
        {"role": "system", "content":
            "[CONTEXTO COMERCIAL DEL MENSAJE SALIENTE - PRIORIDAD]"},
        {"role": "assistant", "content": "Te informamos del nuevo precio."},
        {"role": "user", "content": "¿Me das más información?"},
    ], {"operacion": "Venta"}, write_telemetry=False)
    assert result["respuesta_bot"] == ""
    assert len(calls) == 2
    assert all("response_format" in x for x in calls)
