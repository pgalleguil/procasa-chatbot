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
    source = Path(__file__).resolve().parents[1].joinpath("webhook.py").read_text(encoding="utf-8")
    marker = source.index("    # Intercept only a tester WITH an active/uncertain/opted-out test campaign.")
    assert marker < source.index("    # Para el log de eventos, usamos el número limpio sin el '+'", marker)
    assert marker < source.index("    # Persist to the durable queue.", marker)
    assert 'if is_test_phone(phone):' in source[marker:marker + 1600]
    assert 'should_intercept_test_phone' in source[marker:marker + 1600]
    assert 'if intercept_test:' in source[marker:marker + 1600]
    assert 'no active test campaign; routing tester through normal CRM' in source[marker:marker + 2200]


def test_script_no_longer_looks_up_or_edits_existing_leads():
    source = Path(__file__).resolve().parents[1].joinpath("scripts/price_drop_pilot_test.py").read_text(encoding="utf-8")
    assert 'db["leads"]' not in source
    assert '"test_mode": True' in source
    assert "if not send:" in source
    assert "return run_test_once(send=args.send)" in source


def test_bootstrap_one_shot_is_disabled_by_default():
    source = Path(__file__).resolve().parents[1].joinpath("webhook.py").read_text(encoding="utf-8")
    assert 'price_drop_send_on_start = str(os.getenv("PRICE_DROP_PILOT_SEND_ON_START", "")).lower()' in source
    assert 'PRICE_DROP_PILOT_PREFLIGHT_ON_START' in source
    assert 'price_drop_send_on_start and not price_drop_preflight_on_start' in source
    assert 'lambda: run_test_once(send=send_now)' in source


def test_sender_logs_safe_reason_for_blocked_preflight(isolated, monkeypatch, caplog):
    from scripts import price_drop_pilot_test as sender_script
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", "")

    with caplog.at_level("WARNING"):
        assert sender_script.run_test_once(send=False) == 2

    assert "[PRICE_DROP_PILOT_TEST] blocked reason=recipient_not_configured" in caplog.text


def test_v14_preflight_uses_prop360_normalized_type_and_commune(isolated, monkeypatch, capsys):
    from scripts import price_drop_pilot_test as sender_script
    db = db_case()
    db["crm_price_drop_pilot_v1"].docs = []
    prop = db["universo_cartera_prop360"].docs[0]
    prop["comuna"] = "San Miguel"
    prop["tipo_operacion"]["tipo"] = "Departamento"

    monkeypatch.setattr(sender_script, "get_db", lambda: db)
    calls = []
    monkeypatch.setattr(sender_script, "send_whatsapp_message_detailed_sync",
                        lambda phone, message: calls.append((phone, message)))

    assert sender_script.run_test_once(send=False, version=sender_script.VERSION) == 0
    assert not calls
    assert "departamento de San Miguel" in capsys.readouterr().out


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



def test_v7_sender_fresh_id_and_copy_is_warm_specific_and_historically_grounded():
    from scripts import price_drop_pilot_test as sender
    assert sender.TEST_ID == "20261104-6436-price-drop-pilot-v13-regression-single-test"
    source = Path(__file__).resolve().parents[1].joinpath(
        "scripts", "price_drop_pilot_test.py"
    ).read_text(encoding="utf-8")
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
    assert '"campaign_version": campaign_version' in source
    assert sender.V13_VERSION == "price_drop_pilot_v13_regression_test"
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


def test_v14_sender_dry_run_uses_verified_a_copy_and_no_crm(isolated, monkeypatch, capsys):
    from scripts import price_drop_pilot_test as sender
    db = db_case()
    prop = db["universo_cartera_prop360"].docs[0]
    prop.update({"tipo": "Departamento", "ubicacion": {"comuna": "San Miguel"}})
    db["crm_price_drop_pilot_v1"].docs = []
    monkeypatch.setattr(sender, "get_db", lambda: db)
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_VERSION", sender.VERSION)

    def forbidden_send(*args, **kwargs):
        raise AssertionError("A dry run sent a WhatsApp")

    monkeypatch.setattr(sender, "send_whatsapp_message_detailed_sync", forbidden_send)
    assert sender.run_test_once(send=False) == 0
    output = capsys.readouterr().out
    assert "¡Hola! Te escribimos de PROCASA por el departamento de San Miguel" in output
    assert "Bajó 100 UF: antes estaba en UF 2.790 y ahora está en UF 2.690." in output
    assert "¿Te sigue interesando ese departamento con el nuevo precio?" in output
    assert "Ver el departamento: https://www.procasa.cl/6436" in output
    assert not db["crm_price_drop_pilot_v1"].docs


def test_v14_inbound_uses_prop360_nested_type_and_generates_reply(isolated, monkeypatch):
    from scripts import price_drop_pilot_test as sender
    db = db_case()
    prop = db["universo_cartera_prop360"].docs[0]
    prop["tipo_operacion"]["tipo"] = "Departamento"
    prop["ubicacion"] = {"comuna": "San Miguel"}
    db["crm_price_drop_pilot_v1"].docs = []
    monkeypatch.setattr(sender, "get_db", lambda: db)
    calls = []

    def sent(phone, text):
        calls.append((phone, text))
        return {"success": True, "provider_message_id": "v14-opening-test"}

    monkeypatch.setattr(sender, "send_whatsapp_message_detailed_sync", sent)
    assert sender.run_test_once(send=True, version=sender.VERSION) == 0
    assert len(calls) == 1

    campaign = db["crm_price_drop_pilot_v1"].docs[0]
    assert campaign["campaign_version"] == sender.VERSION
    assert campaign["outbound_message"] == calls[0][1]
    assert campaign["inquiry_evidence_at"].startswith("test-only roleplay:")
    assert campaign["test_mode"] is True
    assert "lead_id" not in campaign
    ctx = load_test_context(db, PHONE)
    assert ctx["campaign_version"] == sender.VERSION
    assert ctx["outbound_message"] == calls[0][1]
    assert ctx["prior_inquiry_verified"] is True
    assert ctx["property_type"] == "Departamento"
    assert ctx["property_facts"]["comuna"] == "San Miguel"

    reply_calls = []
    def reply_generator(messages, operation_context, *, write_telemetry,
                        diagnostics, retry_invalid_json):
        diagnostics.update({
            "provider": "fixture", "model": "fixture-model",
            "provider_calls": 1, "format_retries": 0,
            "raw_responses": [], "usage": None, "cost": None,
        })
        assert messages[-1] == {
            "role": "user", "content": "¿Me puedes contar más del departamento?"
        }
        assert operation_context["tipo"] == "Departamento"
        assert operation_context["comuna"] == "San Miguel"
        return {"respuesta_bot": "Claro, te ayudo con las dudas sobre el departamento."}

    def reply_sender(phone, text):
        reply_calls.append((phone, text))
        return {"success": True, "provider_message_id": "v14-reply-test"}

    outcome = process_test_inbound(
        db, PHONE, "v14-inbound-test", "¿Me puedes contar más del departamento?",
        sender=reply_sender, commercial_generator=reply_generator,
    )
    assert outcome == {"status": "test_reply_sent"}
    assert len(reply_calls) == 1
    assert reply_calls[0][1] == "Claro, te ayudo con las dudas sobre el departamento."
    turn = db["crm_price_drop_pilot_test_turns_v1"].docs[0]
    assert turn["status"] == "SENT"
    assert turn["candidate_audit"]["errors"] == []

    assert sender.run_test_once(send=True, version=sender.VERSION) == 2
    assert len(calls) == 1



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
    assert "calle_publicada" not in context["property_facts"]
    assert context["property_facts"]["parrillas_ficha"] is True
    send, calls = sender_spy()
    conversation = [
        ("Podria ser pero donde quedaba", "San Miguel"),
        ("Tiene terraza?", "5 m² de terraza"),
        ("Y quinchos", "parrillas mencionadas en la ficha"),
        ("Tienes la direccion exacta", "San Miguel"),
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
    assert "Séptima Avenida 1370 68" not in calls[-1][1]
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
    assert "San Miguel" in address and "dirección exacta no disponible" in address
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
    assert ctx["price_drop_date"] is None
    answer = build_native_pilot_reply(
        customer_message, ctx, [], generator=lambda *_args, **_kw: (
            _ for _ in ()
        ).throw(AssertionError("No LLM should handle an audited date"))
    )
    assert "fecha exacta" in answer and "6 de octubre" not in answer
    assert "no tengo" in answer.lower()
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
    assert "6 de octubre de 2026" not in msg[1]["content"]
    assert msg[2]["role"] == "assistant"
    assert "Te escribo porque el departamento" in msg[2]["content"]
    sender, calls = sender_spy()
    # No model needed for date question and only ONE outbound provider call.
    assert process_test_inbound(
        db, PHONE, "date-e2e-v6", "CUANDO BAJO",
        sender=sender,
    )["status"] == "test_reply_sent"
    assert len(calls) == 1
    assert "fecha exacta" in calls[0][1]
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


def test_campaign_greeting_avoids_resending_link_unless_customer_requests_it(
    isolated, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "ci-offline-test")
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "true")
    from chatbot import grok_client
    db = db_case()
    calls = []
    def simulated_model(messages, prospect, **kwargs):
        calls.append(messages)
        return {"intencion": "consulta_general", "datos_extraidos": {},
                "respuesta_bot": (
                    "Te dejé el enlace https://www.procasa.cl/6436."
                    if len(calls) == 1 else
                    "¡Hola! Gracias por responder 😊. ¿Qué tal te pareció la rebaja?"
                )}
    monkeypatch.setattr(grok_client, "generar_respuesta_estructurada", simulated_model)
    sender, outgoing = sender_spy()
    assert process_test_inbound(
        db, PHONE, "greeting-no-url", "HOLA", sender=sender
    )["status"] == "test_reply_sent"
    assert len(calls) == 2
    assert len(outgoing) == 1
    assert "https://www.procasa.cl" not in outgoing[0][1]
    assert "Gracias por responder" in outgoing[0][1]

    def requested_link(messages, prospect, **kwargs):
        return {"intencion": "consulta_general", "datos_extraidos": {},
                "respuesta_bot": "Claro, aquí está: https://www.procasa.cl/6436"}
    monkeypatch.setattr(grok_client, "generar_respuesta_estructurada", requested_link)
    assert process_test_inbound(
        db, PHONE, "link-requested", "¿Me compartes el enlace?", sender=sender
    )["status"] == "test_reply_sent"
    assert "https://www.procasa.cl/6436" in outgoing[1][1]
    assert len(outgoing) == 2


# A conversation is a sequence of user messages; every message runs through
# the isolated production handler, while the DeepSeek API and WhatsApp send
# are replaced by bounded offline doubles. Real lead records stay inaccessible.
import json as _v8_json
_V8_SCENARIOS = _v8_json.loads(
    (Path(__file__).parent / "fixtures" /
     "price_drop_v8_twenty_dialogues.json").read_text(encoding="utf-8")
)
assert len(_V8_SCENARIOS) == 20
assert sum(len(turns) for _, turns in _V8_SCENARIOS) == 121


def _v8_offline_model(messages, prospect, **kwargs):
    """Adversarial model stand-in to exercise output validation and memory."""
    from chatbot.price_drop_pilot_isolation import _plain
    user = _plain(messages[-1]["content"]).strip()
    earlier = " ".join(
        _plain(x["content"]) for x in messages[:-1]
        if x.get("role") == "user"
    )
    if user in ("hola", "buenas"):
        # Reproduce the actual V7 failure: full catalogue on a mere greeting.
        reply = (
            "¡Hola de nuevo! Te resumo: 2 dormitorios, 2 baños, 65 m², "
            "terraza, bodega, piscina y piso 6. Su nuevo precio es UF 2.690. "
            "¿Te gustaría agendar una visita, ver la publicación o recibir "
            "una ficha con todas las características?"
        )
    elif "nadie" in user or "no me contacto" in user:
        reply = "Lamento la situación. Lo dejo planteado al equipo."
    elif "asesor" in user or "me van a llamar" in user:
        reply = "Gracias por consultarlo. Lo dejo planteado al equipo."
    elif "quiero visitar" in user or "me interesa" in user:
        reply = "Si te interesa una visita, dime qué día u horario te acomoda."
    elif user == "bueno" and "nadie" in earlier:
        reply = "Si quieres una visita, dime qué día u horario te acomoda."
    elif user.startswith("gracias"):
        reply = "¡De nada! 😊 Aquí estoy si surge otra consulta."
    elif user in ("ok", "bueno", "perfecto"):
        reply = "Perfecto 😊. Te leo."
    elif "enlace" in user or "link" in user:
        reply = "Claro, aquí está la publicación: https://www.procasa.cl/6436"
    elif "dormitorios" in user:
        reply = "Tiene 2 dormitorios, según la ficha."
    elif "bano" in user:
        reply = "La ficha registra 2 baños."
    elif "terraza" in user:
        reply = "Sí, cuenta con terraza según la publicación."
    elif "piscina" in user:
        reply = "Sí, la ficha del edificio menciona piscina."
    elif "bodega" in user:
        reply = "Sí, se informa una bodega."
    elif "me escribieron" in user:
        reply = "Sí, PROCASA te escribió primero por el aviso de rebaja."
    else:
        reply = (
            "No tengo otro dato confirmado sobre ese punto en la ficha, "
            "así que prefiero no inventar una respuesta."
        )
    return {"intencion": "consulta_general", "datos_extraidos": {},
            "respuesta_bot": reply}


@pytest.mark.parametrize(
    "scenario_name,turns", _V8_SCENARIOS,
    ids=[name for name, _ in _V8_SCENARIOS],
)
def test_v8_twenty_realistic_offline_customer_conversations(
    isolated, monkeypatch, scenario_name, turns
):
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "ci-test-no-network")
    from chatbot import grok_client
    from chatbot.price_drop_pilot_isolation import _plain, _GREETING
    db = db_case()
    prop = db["universo_cartera_prop360"].docs[0]
    prop["tipo_operacion"]["gastos_comunes"] = 85000
    prop["caracteristicas"] = {
        "dormitorios": 2, "banos": 2, "bodegas": 1,
        "superficie_total": 65, "superficie_terraza": 5,
        "piso": 6, "features": ["piscina", "has_terrace"],
    }
    prop["historial_cambios"] = [{
        "fecha": "2026-10-06T21:55:59-03:00",
        "campo": "precio_uf", "valor_anterior": 2790,
        "valor_nuevo": 2690, "fuente": "prop360_bitacora",
    }]
    seen = []
    def model(messages, prospect, **kwargs):
        seen.append(messages)
        return _v8_offline_model(messages, prospect, **kwargs)
    monkeypatch.setattr(
        grok_client, "generar_respuesta_estructurada", model
    )
    send, delivered = sender_spy()
    for i, customer in enumerate(turns):
        outcome = process_test_inbound(
            db, PHONE, f"{scenario_name}-{i}", customer, sender=send
        )
        if customer.casefold() == "no me escriban más":
            assert outcome == {"status": "test_stopped"}
            assert len(delivered) == i
            break
        assert outcome == {"status": "test_reply_sent"}, (scenario_name, customer, outcome)
        reply = delivered[-1][1]
        low = _plain(reply)
        question = _plain(customer)
        assert reply and len(reply) < 650
        assert "lo dejo planteado" not in low
        assert "te llamara" not in low
        assert "visita confirmada" not in low
        assert "es competitivo" not in low
        if _GREETING.fullmatch(customer):
            assert len(reply) <= 175
            assert "dormitorios" not in low
            assert "https://www.procasa.cl/" not in reply
        if "disponib" in question or "disponob" in question:
            assert "disponible" in low and "confirmar" in low
        if "gastos comunes" in question or "gastos comunes" in low and "ggcc" in question:
            assert "$85.000" in reply
        if "variacion" in question or "cuanto bajo" in question:
            assert "100" in reply
        if "cuando" in question and ("baj" in question or "rebaj" in question):
            assert "fecha exacta" in reply and "6 de octubre" not in reply
        if "10 o 11" in question or "10 o a las 11" in question:
            assert "10 o a las 11" in low
            assert "entre 10 y 11" not in low
            assert "no se ha" in low or "no hay" in low
        if "nadie" in question:
            assert "no se ha enviado" in low or "no se ha solicitado" in low
        if question in ("no estoy seguro", "no puedo esta semana"):
            assert "gracias por responder" not in low
        if "no habia consultado" in question:
            assert "no tengo" in low and "verificada" in low
        if "cuanto cuesta ahora" in question or "cuanto sale" in question:
            assert "2.690" in reply
        if "que porcentaje bajo" in question:
            assert "3,58%" in reply
        if ("hola soy" in question or "hola, soy" in question) and i == 0:
            assert "gracias por responder" in low
        if scenario_name == "repeat_greetings" and i in (1, 2):
            assert reply != delivered[-2][1]
    assert len(db["crm_price_drop_pilot_test_turns_v1"].docs) == len(turns)
    assert len(delivered) == len(turns) - int(
        turns[-1].casefold() == "no me escriban más"
    )
    assert all(number == PHONE for number, _ in delivered)
    assert set(db) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1",
    }
    if len(seen) > 1:
        # The model gets the original outbound and subsequent history.
        last = seen[-1]
        assert last[2]["role"] == "assistant"
        assert "Te escribo porque el departamento" in last[2]["content"]
        assert any(m["role"] == "user" for m in last[3:-1])


def test_live_v8_no_unverified_apartment_number_in_realistic_reply(isolated):
    """Reproduce the first real V8 reply: trailing '68' is not a verified unit."""
    from chatbot.price_drop_pilot_isolation import (
        _sanitize_unverified_unit, build_native_pilot_reply,
    )
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["ubicacion"] = {
        "calle": "Séptima Avenida 1370 68",
        "comuna": "San Miguel",
        "unidad": None,
    }
    ctx = load_test_context(db, PHONE)
    assert ctx["property_facts"].get("unidad_verificada") is None
    raw = (
        "Claro 😊 Es el departamento en San Miguel, en Séptima Avenida 1370, "
        "departamento 68. Tiene 2 dormitorios y 65 m²."
    )
    assert "departamento 68" not in _sanitize_unverified_unit(raw, ctx)
    answer = build_native_pilot_reply(
        "Comentame no recuerdonesa casa", ctx, [],
        generator=lambda *_args, **_kwargs: {
            "intencion": "consulta_general",
            "datos_extraidos": {},
            "respuesta_bot": raw,
        },
    )
    assert "Séptima Avenida 1370" not in answer
    assert "1370" not in answer
    assert "departamento 68" not in answer
    assert "San Miguel" in answer
    assert "ubicación exacta" in answer or "dirección exacta" in answer


def test_explicit_prop360_unit_is_always_denied_to_pilot(isolated):
    from chatbot.price_drop_pilot_isolation import _sanitize_unverified_unit
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["ubicacion"] = {
        "calle": "Séptima Avenida 1370",
        "comuna": "San Miguel",
        "unidad": "68",
    }
    ctx = load_test_context(db, PHONE)
    assert "unidad_verificada" not in ctx["property_facts"]
    reply = "Está en Séptima Avenida 1370, departamento 68."
    assert "departamento 68" not in _sanitize_unverified_unit(reply, ctx)
    assert "departamento 69" not in _sanitize_unverified_unit(
        "Está en Séptima Avenida 1370, departamento 69.", ctx
    )


@pytest.mark.parametrize("query", [
    "Tienes la direccion exacta?",
    "Yvla direcion exacta",
    "¿Dónde queda el departamento?",
])
def test_campaign_location_shows_area_not_street_number(isolated, query):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["ubicacion"] = {
        "calle": "Séptima Avenida 1370 68",
        "direccion_referencial": "Séptima Avenida - Tercera Transversal",
        "comuna": "San Miguel",
        "unidad": None,
    }
    ctx = load_test_context(db, PHONE)
    answer = build_native_pilot_reply(
        query, ctx, [],
        generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("A verified area is answered without the model")
        ),
    )
    assert "Séptima Avenida - Tercera Transversal" not in answer
    assert "San Miguel" in answer
    assert "1370" not in answer and "departamento 68" not in answer


def test_campaign_acknowledges_address_and_commission_objection(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    db = db_case()
    ctx = load_test_context(db, PHONE)
    answer = build_native_pilot_reply(
        "Mo deberias darme la direccion porque iria sin corredor y sin pagar 2% de comision",
        ctx, [],
        generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Address objection should not need the LLM")
        ),
    )
    assert "Entiendo" in answer
    assert "corretaje" in answer
    assert "2%" not in answer
    assert "no quiero darte información" not in answer


def test_campaign_guards_exact_address_leaked_by_dialogue_history(isolated):
    from chatbot.price_drop_pilot_isolation import (
        build_native_pilot_reply, _build_native_campaign_messages,
    )
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["ubicacion"] = {
        "calle": "Séptima Avenida 1370 68",
        "direccion_referencial": "Séptima Avenida - Tercera Transversal",
        "comuna": "San Miguel",
        "unidad": None,
    }
    ctx = load_test_context(db, PHONE)
    history = [{
        "customer_text": "¿Y la dirección exacta?",
        "bot_text": "En Séptima Avenida 1370, departamento 68.",
    }]
    msgs = _build_native_campaign_messages(
        "¿Cómo es el sector?", ctx, history
    )
    assert "dirección publicada (texto literal)" not in msgs[1]["content"]
    assert "1370" not in msgs[1]["content"]
    answer = build_native_pilot_reply(
        "¿Cómo es el sector?", ctx, history,
        generator=lambda *_a, **_k: {
            "intencion": "consulta_general", "datos_extraidos": {},
            "respuesta_bot": "Está en Séptima Avenida 1370, departamento 68.",
        },
    )
    assert "1370" not in answer
    assert "departamento 68" not in answer
    assert "Tercera Transversal" not in answer


@pytest.mark.parametrize("customer", [
    "Tienes la direccion exacta?",
    "Yvla direcion exacta",
    "¿Dónde queda?",
    "¿Cuál es la ubicación?",
    "¿En qué calle está?",
    "Mo deberias darme la direccion porque sino yo iria solo sin corresro y sin pagar comision",
])
def test_v8_campaign_location_objections_never_reveal_street_number(
    isolated, customer
):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["ubicacion"] = {
        "calle": "Séptima Avenida 1370 68",
        "unidad": None,
        "direccion_referencial": "Séptima Avenida - Tercera Transversal",
        "comuna": "San Miguel",
    }
    ctx = load_test_context(db, PHONE)
    reply = build_native_pilot_reply(
        customer, ctx, [],
        generator=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Location/objection should not require the model")
        ),
    )
    assert "1370" not in reply
    assert "departamento 68" not in reply.lower()
    assert ("sector" in reply.lower() or
            "dirección exacta" in reply.lower())
    assert "San Miguel" in reply or "corretaje" in reply


@pytest.mark.parametrize("model_reply", [
    "La propiedad está en Séptima Avenida 1370, departamento 68, San Miguel.",
    "Claro, es Séptima Avenida 1370 68, piso 6.",
    "Se ubica en Séptima Avenida 1370, dpto 68. Te leo.",
    "Está en Av. Séptima 1370, en San Miguel.",
    "La dirección es Av. Séptima 1370.",
    "El número es 1370, justo cerca de la transversal.",
])
def test_v8_model_cannot_repeat_address_from_previous_customer_turn(
    isolated, model_reply
):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["ubicacion"] = {
        "calle": "Séptima Avenida 1370 68",
        "unidad": None,
        "direccion_referencial": "Séptima Avenida - Tercera Transversal",
        "comuna": "San Miguel",
    }
    ctx = load_test_context(db, PHONE)
    prior = [
        {"customer_text": "Tienes la direccion exacta?",
         "bot_text": "La dirección es Séptima Avenida 1370, departamento 68."},
    ]
    reply = build_native_pilot_reply(
        "Comentame no recuerdonesa casa", ctx, prior,
        generator=lambda *_args, **_kwargs: {
            "intencion": "consulta_general",
            "datos_extraidos": {},
            "respuesta_bot": model_reply,
        },
    )
    assert "1370" not in reply
    assert "departamento 68" not in reply.lower()
    assert "Tercera Transversal" not in reply
    assert "San Miguel" in reply


def test_v8_location_without_reference_discloses_only_commune(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["ubicacion"] = {
        "calle": "Séptima Avenida 1370 68", "unidad": None,
        "comuna": "San Miguel",
    }
    ctx = load_test_context(db, PHONE)
    reply = build_native_pilot_reply(
        "La direccion por favor", ctx, [],
        generator=lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(AssertionError("Must not call LLM")),
    )
    assert "San Miguel" in reply
    assert "1370" not in reply


def test_v8_real_whatsapp_ten_turn_replay_avoids_old_address_and_crm(
    isolated, monkeypatch
):
    from chatbot import grok_client
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["ubicacion"] = {
        "calle": "Séptima Avenida 1370 68",
        "unidad": None,
        "direccion_referencial": "Séptima Avenida - Tercera Transversal",
        "comuna": "San Miguel",
    }
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "ci-no-network")
    sender, outgoing = sender_spy()

    def fake_model(messages, *_args, **_kwargs):
        # Adversarial imitation of the V8 mistake in the exact channel.
        return {"intencion": "consulta_general", "datos_extraidos": {},
                "respuesta_bot": (
                    "Claro, el departamento está en Séptima Avenida 1370, "
                    "departamento 68. Tiene 2 dormitorios y 2 baños."
                )}
    monkeypatch.setattr(
        grok_client, "generar_respuesta_estructurada", fake_model
    )
    replay = [
        "Comentame no recuerdonesa casa",
        "Si lonrecuerdo en realidad tengo un presupuesto menos aceptabofertas?",
        "Cual es su orientacion",
        "Tienes la direccion exacta?",
        "Mo deberias darme la direccion porque sino yo iria solo sin corresro y sinnpagar por 2% de comision",
        "Me interesa visidtarlo",
        "El fin de semana dependie dipobibilidsd de ustedes durante en la mañana",
        "Me puedes dar el teléfono del propietario",
        "Ybel nombre de propietario",
        "Yvla direcion exacta",
    ]
    for i, customer in enumerate(replay):
        result = process_test_inbound(
            db, PHONE, f"v8-live-history-address-fix-{i}", customer,
            sender=sender,
        )
        assert result["status"] == "test_reply_sent"
        assert "1370" not in outgoing[-1][1]
        assert "departamento 68" not in outgoing[-1][1].lower()
    assert len(outgoing) == len(replay)
    assert len(db["crm_price_drop_pilot_test_turns_v1"].docs) == len(replay)
    assert set(db) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1",
    }


def test_v8_numbered_location_reference_never_leaks_address(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["ubicacion"] = {
        "calle": "Séptima Avenida 1370 68",
        "direccion_referencial": "Séptima Avenida 1370 - Tercera Transversal",
        "comuna": "San Miguel",
        "unidad": None,
    }
    ctx = load_test_context(db, PHONE)
    output = build_native_pilot_reply(
        "¿Dónde queda?", ctx, [],
        generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Address query should not invoke model")
        ),
    )
    assert "San Miguel" in output
    assert "1370" not in output
    assert "sector de Séptima Avenida 1370" not in output

def test_v8_mixed_location_and_visit_keeps_both_intents(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    ctx = load_test_context(db_case(), PHONE)
    answer = build_native_pilot_reply(
        "¿Dónde queda y puedo visitarlo el sábado a las 11?",
        ctx, [],
        generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Verified mixed intent should not need a model")
        ),
    )
    assert "San Miguel" in answer
    assert "sábado" in answer
    assert "11" in answer
    assert "no hay" in answer.lower() and "agendada" in answer.lower()


def test_v8_location_mention_is_not_a_request_to_repeat_it(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    ctx = load_test_context(db_case(), PHONE)
    answer = build_native_pilot_reply(
        "No me repitas la dirección. Quiero visitarlo el sábado a las 11.",
        ctx, [],
        generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Visit preference should be handled directly")
        ),
    )
    assert "sábado" in answer and "11" in answer
    assert "dirección" not in answer.lower()
    assert "San Miguel" not in answer


def test_v8_mixed_location_and_price_answers_both(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    ctx = load_test_context(db_case(), PHONE)
    answer = build_native_pilot_reply(
        "¿Dónde queda y cuánto cuesta ahora?",
        ctx, [],
        generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Both verified facts should be direct")
        ),
    )
    assert "San Miguel" in answer
    assert "2.690" in answer

def test_v8_mixed_location_visit_without_hour_still_advances(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    ctx = load_test_context(db_case(), PHONE)
    answer = build_native_pilot_reply(
        "¿Dónde queda? Quiero visitarlo el sábado.", ctx, [],
        generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Mixed visit and location should be deterministic")
        ),
    )
    assert "San Miguel" in answer
    assert "sábado" in answer
    assert "mañana o en la tarde" in answer


def test_v8_location_optout_visit_without_hour_does_not_repeat_location(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    ctx = load_test_context(db_case(), PHONE)
    answer = build_native_pilot_reply(
        "No me repitas la dirección, quiero visitarlo el sábado.", ctx, [],
        generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Visit and location opt-out should be deterministic")
        ),
    )
    assert "sábado" in answer
    assert "San Miguel" not in answer
    assert "dirección" not in answer.lower()

def test_v8_mixed_location_price_visit_answers_all_three(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    ctx = load_test_context(db_case(), PHONE)
    answer = build_native_pilot_reply(
        "¿Dónde queda, cuánto cuesta y puedo visitarlo el sábado a las 11?",
        ctx, [],
        generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Triple intent should be deterministic")
        ),
    )
    assert "San Miguel" in answer
    assert "2.690" in answer
    assert "sábado" in answer and "11" in answer
    assert "agendada" in answer

def test_v10_projection_does_not_fetch_any_sensitive_property_location_fields(isolated):
    from chatbot.price_drop_pilot_isolation import load_test_context
    db = _full_pilot_db()
    property_collection = db["universo_cartera_prop360"]
    property_collection.docs[0].setdefault("ubicacion", {})["comuna"] = "San Miguel"
    observed = []
    old_find = property_collection.find_one

    def checked_find(query, projection=None, sort=None):
        observed.append(dict(projection or {}))
        assert projection and all(value == 1 for value in projection.values())
        forbidden = (
            "ubicacion.calle", "ubicacion.direccion_referencial",
            "ubicacion.unidad", "ubicacion.sector", "historial_cambios",
            "observaciones", "descripcion", "propietario", "telefono",
        )
        assert all(key not in projection for key in forbidden)
        return old_find(query, projection, sort=sort)

    property_collection.find_one = checked_find
    ctx = load_test_context(db, PHONE)
    assert observed and ctx["property_facts"]["comuna"] == "San Miguel"
    assert all(k not in ctx["property_facts"] for k in (
        "calle_publicada", "direccion_referencial", "unidad_verificada",
    ))


def test_v10_sensitive_location_values_are_never_accessed_in_python(isolated):
    from chatbot.price_drop_pilot_isolation import load_test_context

    class LockedLocation(dict):
        def get(self, name, default=None):
            if name in {"calle", "direccion_referencial", "unidad", "sector"}:
                raise AssertionError("Sensitive location read")
            return super().get(name, default)

    db = _full_pilot_db()
    db["universo_cartera_prop360"].docs[0]["ubicacion"] = LockedLocation({
        "comuna": "San Miguel", "calle": "SECRET-STREET-7382",
        "direccion_referencial": "SECRET-REFERENCE-7382", "unidad": "999",
    })
    ctx = load_test_context(db, PHONE)
    assert "SECRET" not in str(ctx)
    assert ctx["property_facts"]["comuna"] == "San Miguel"


def test_v10_real_dialogue_5am_is_not_misread_as_5pm(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    ctx = load_test_context(db_case(), PHONE)
    reply = build_native_pilot_reply(
        "Quiero visidta mañana a las 5 de la mañana", ctx, [],
        generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Early visit time should be handled without model")
        ),
    )
    assert "05:00" in reply
    assert "poco habitual" in reply
    assert "no hay visita agendada" in reply.lower()


def test_v10_real_dialogue_personal_question_and_ambiguous_orientation(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    ctx = load_test_context(db_case(), PHONE)
    personal = build_native_pilot_reply(
        "Tienew novio", ctx, [], generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Must not anthropomorphize")
        ),
    )
    assert "asistente virtual" in personal
    orientation = build_native_pilot_reply(
        "Cualnes su orientacion", ctx,
        [{"customer_text": "Tienew novio", "bot_text": personal}],
        generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Ambiguous personal context should clarify")
        ),
    )
    assert "¿Te refieres" in orientation
    assert "Sur-Poniente" not in orientation


def test_v10_contact_request_never_promises_an_executive_call(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    ctx = load_test_context(db_case(), PHONE)
    reply = build_native_pilot_reply(
        "Quien me contactará", ctx, [],
        generator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Contact handoff must be deterministic")
        ),
    )
    assert "no hay visita ni contacto programado" in reply
    assert "te llamará" not in reply


def test_v10_repeated_exact_address_question_advances_without_street(isolated):
    from chatbot.price_drop_pilot_isolation import build_native_pilot_reply
    db = db_case()
    db["universo_cartera_prop360"].docs[0]["ubicacion"] = {
        "comuna": "San Miguel", "calle": "SECRET-AVENUE-7382",
        "direccion_referencial": "SECRET-LOCATION", "unidad": "999",
    }
    ctx = load_test_context(db, PHONE)
    first = build_native_pilot_reply("Tienws la direcion de la propiedad", ctx, [])
    second = build_native_pilot_reply(
        "Direccion exacta", ctx,
        [{"customer_text": "Tienws la direcion de la propiedad", "bot_text": first}],
    )
    assert first != second
    assert "San Miguel" in second
    assert "SECRET" not in first + second


def test_v10_prompt_redacts_historical_address_before_model(isolated):
    from chatbot.price_drop_pilot_isolation import _build_native_campaign_messages
    ctx = load_test_context(db_case(), PHONE)
    msgs = _build_native_campaign_messages(
        "Cuéntame", ctx,
        [{"customer_text": "La calle es Secret Avenue 1234",
          "bot_text": "Está en Séptima Avenida 1370, dpto 68"}],
    )
    content = " ".join(str(m["content"]) for m in msgs)
    assert "1370" not in content and "1234" not in content
    assert "Séptima Avenida" not in content



def _v10_sim_setup(monkeypatch):
    from chatbot.price_drop_pilot_exec_notice_sim import VERSION
    monkeypatch.setenv("PRICE_DROP_PILOT_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", PHONE)
    monkeypatch.setenv("PRICE_DROP_PILOT_EXEC_NOTICE_SIMULATION", "true")
    db = db_case()
    campaign = db["crm_price_drop_pilot_v1"].docs[0]
    campaign["campaign_version"] = VERSION
    history = {
        "_id": "prior-turn",
        "campaign_id": campaign["_id"],
        "customer_text": "Gracias, me interesa conocer más",
        "bot_text": "¿Te gustaría retomar el departamento con el precio rebajado?",
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        "status": "SENT",
    }
    db["crm_price_drop_pilot_test_turns_v1"].docs.append(history)
    return db


def test_v10_exec_sim_notification_sent_only_to_whitelisted_tester_after_yes(monkeypatch):
    db = _v10_sim_setup(monkeypatch)
    answer_sender, answer_calls = sender_spy()
    simulated = []
    def exec_sender(phone, text):
        simulated.append((phone, text))
        return {"success": True, "provider_message_id": "simulated-exec-notice-1"}
    out = process_test_inbound(
        db, PHONE, "v10-affirmative-1", "Sí",
        sender=answer_sender, exec_notice_sender=exec_sender,
    )
    assert out == {"status": "test_reply_sent", "test_exec_notice_status": "SENT"}
    assert len(answer_calls) == 1
    assert len(simulated) == 1
    assert simulated[0][0] == PHONE
    assert "AVISO SIMULADO AL EJECUTIVO" in simulated[0][1]
    assert "NO" in simulated[0][1] or "No" in simulated[0][1]
    assert "UF 2.790" in simulated[0][1]
    assert "UF 2.690" in simulated[0][1]
    assert "no disponible en esta prueba aislada" in simulated[0][1]
    assert "no a un ejecutivo" in simulated[0][1]
    assert db["crm_price_drop_pilot_test_turns_v1"].docs[-1]["status"] == "EXEC_NOTICE_SENT"
    assert "crm_assignment_cycles" not in db


def test_v10_exec_sim_only_after_last_relevant_question(monkeypatch):
    db = _v10_sim_setup(monkeypatch)
    simulated = []
    reply_sender, reply_calls = sender_spy()
    exec_sender = lambda *x: simulated.append(x)
    # Not an affirmative. Conversation continues, no notification.
    out = process_test_inbound(
        db, PHONE, "v10-hello", "¿Cuántos dormitorios tiene?",
        sender=reply_sender, exec_notice_sender=exec_sender,
    )
    assert out == {"status": "test_reply_sent"}
    assert simulated == []
    assert len(reply_calls) == 1


def test_v10_exec_sim_requires_prior_question_and_never_sends_for_bare_yes(monkeypatch):
    db = _v10_sim_setup(monkeypatch)
    db["crm_price_drop_pilot_test_turns_v1"].docs = []
    exec_calls = []
    answer_sender, _ = sender_spy()
    assert process_test_inbound(
        db, PHONE, "v10-bare-yes", "Sí",
        sender=answer_sender, exec_notice_sender=lambda *x: exec_calls.append(x),
    )["status"] == "test_reply_sent"
    assert exec_calls == []


def test_v10_exec_sim_does_not_run_for_old_v9_conversation(monkeypatch):
    db = _v10_sim_setup(monkeypatch)
    db["crm_price_drop_pilot_v1"].docs[0]["campaign_version"] = "price_drop_pilot_v9"
    sent = []
    answer_sender, _ = sender_spy()
    assert process_test_inbound(
        db, PHONE, "v9-yes", "Sí",
        sender=answer_sender, exec_notice_sender=lambda *x: sent.append(x),
    )["status"] == "test_reply_sent"
    assert sent == []


def test_v10_exec_simulator_never_retries_ambiguous_provider_delivery(monkeypatch):
    from chatbot.price_drop_pilot_exec_notice_sim import send_simulated_exec_notice
    db = _v10_sim_setup(monkeypatch)
    ctx = load_test_context(db, PHONE)
    history = list(db["crm_price_drop_pilot_test_turns_v1"].docs)
    called = []
    def sender(*_):
        called.append(1)
        raise TimeoutError("provider status unknown")
    x = send_simulated_exec_notice(
        db, ctx=ctx, phone=PHONE, customer_text="Sí", history=history,
        inbound_message_id="yes-one", sender=sender,
    )
    assert x["status"] == "DELIVERY_UNKNOWN"
    y = send_simulated_exec_notice(
        db, ctx=ctx, phone=PHONE, customer_text="Sí", history=history,
        inbound_message_id="yes-another", sender=sender,
    )
    assert y["status"] == "ALREADY_ATTEMPTED"
    assert called == [1]


def test_v10_exec_sim_test_sent_no_new_crm_lead_and_no_real_exec_lookup(monkeypatch):
    db = _v10_sim_setup(monkeypatch)
    s, _ = sender_spy()
    exec_sender, _ = sender_spy()
    out = process_test_inbound(
        db, PHONE, "another-yes", "Sí", sender=s,
        exec_notice_sender=exec_sender,
    )
    assert out["test_exec_notice_status"] == "SENT"
    assert set(db.keys()) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1"
    }



@pytest.mark.parametrize("customer_text,expected", [
    ("OK, ME INTERESA", True),
    ("Sí, el día domingo durante la mañana", True),
    ("Me gustaría coordinar una visita", True),
    ("Quiero que me llamen", True),
    ("Quiero que me llame un ejecutivo y quiero saber el precio", True),
    ("No me interesa", False),
    ("Solo quiero saber cuánto cuesta", False),
    ("¿Cuántos dormitorios?", False),
    ("hola", False),
])
def test_v10_natural_commercial_detection(customer_text, expected):
    from chatbot.price_drop_pilot_exec_notice_sim import should_simulate
    history = [{
        "bot_text": "El ejecutivo puede mostrarte el departamento. "
                    "¿Tienes algún día o momento de la semana que te acomode más?",
    }]
    assert should_simulate(customer_text, history) is expected


def test_v10_natural_interest_sends_simulated_notice_to_tester_not_executive(monkeypatch):
    db = _v10_sim_setup(monkeypatch)
    bot_sender, bot_calls = sender_spy()
    executive_notices = []
    def simulated_only_to_test_phone(phone, message):
        executive_notices.append((phone, message))
        return {"success": True, "provider_message_id": "sim-natural-1"}
    result = process_test_inbound(
        db, PHONE, "natural-interest-1", "OK, ME INTERESA",
        sender=bot_sender, exec_notice_sender=simulated_only_to_test_phone,
    )
    assert result["test_exec_notice_status"] == "SENT"
    assert len(bot_calls) == 1
    assert len(executive_notices) == 1
    assert executive_notices[0][0] == PHONE
    assert "OK, ME INTERESA" in executive_notices[0][1]
    assert "PRUEBA INTERNA" in executive_notices[0][1]
    assert "no disponible en esta prueba aislada" in executive_notices[0][1]
    assert db["crm_price_drop_pilot_test_turns_v1"].docs[-1]["status"] == "EXEC_NOTICE_SENT"
    assert "crm_assignment_cycles" not in db


def test_v10_one_time_recovery_recovers_prior_unalerted_interest(monkeypatch):
    from chatbot.price_drop_pilot_exec_notice_sim import recover_v10_exec_notice_once
    db = _v10_sim_setup(monkeypatch)
    history = db["crm_price_drop_pilot_test_turns_v1"].docs
    history[0]["customer_text"] = "¿Cuánto bajó?"
    history[0]["bot_text"] = "Bajó 100 UF. ¿Te gustaría saber más?"
    history.append({
        "_id": "historic-positive-yes",
        "campaign_id": db["crm_price_drop_pilot_v1"].docs[0]["_id"],
        "customer_text": "OK, ME INTERESA",
        "provider_inbound_id": "provider-historic-yes",
        "bot_text": "Perfecto, ¿qué día te acomoda?",
        "created_at": datetime.now(timezone.utc),
        "status": "SENT",
    })
    sends = []
    def sender(phone, message):
        sends.append((phone, message))
        return {"success": True, "provider_message_id": "recovery-msg-1"}
    first = recover_v10_exec_notice_once(db, sender=sender)
    assert first["status"] == "SENT"
    assert first["reason"] == "recovered_positive_v10_reply"
    assert len(sends) == 1
    assert sends[0][0] == PHONE
    assert "OK, ME INTERESA" in sends[0][1]
    assert "UF 2.790" in sends[0][1]
    assert "no a un ejecutivo" in sends[0][1]
    second = recover_v10_exec_notice_once(db, sender=sender)
    assert second["status"] == "ALREADY_ATTEMPTED"
    assert len(sends) == 1
    assert "leads" in db and "crm_assignment_cycles" not in db


def test_v10_recovery_off_or_unwhitelisted_never_sends(monkeypatch):
    from chatbot.price_drop_pilot_exec_notice_sim import recover_v10_exec_notice_once, FLAG
    db = _v10_sim_setup(monkeypatch)
    sent = []
    monkeypatch.delenv(FLAG)
    assert recover_v10_exec_notice_once(
        db, sender=lambda *x: sent.append(x),
    )["status"] == "DISABLED"
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", "+56977778888")
    assert recover_v10_exec_notice_once(
        db, sender=lambda *x: sent.append(x),
    )["status"] != "SENT"
    assert not sent


def test_v10_recovery_does_not_send_for_old_campaign(monkeypatch):
    from chatbot.price_drop_pilot_exec_notice_sim import recover_v10_exec_notice_once
    db = _v10_sim_setup(monkeypatch)
    db["crm_price_drop_pilot_v1"].docs[0]["campaign_version"] = "price_drop_pilot_v9"
    sent = []
    result = recover_v10_exec_notice_once(
        db, sender=lambda *x: sent.append(x),
    )
    assert result["status"] != "SENT"
    assert sent == []


def _v11_sim_setup(monkeypatch):
    from chatbot.price_drop_pilot_dual_notice_sim import VERSION
    monkeypatch.setenv("PRICE_DROP_PILOT_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", PHONE)
    monkeypatch.setenv("PRICE_DROP_PILOT_EXEC_NOTICE_SIMULATION", "true")
    db = db_case()
    db["crm_price_drop_pilot_v1"].docs[0]["campaign_version"] = VERSION
    return db


def test_v11_new_chat_separate_admin_exec_messages_to_tester(monkeypatch):
    db = _v11_sim_setup(monkeypatch)
    bot_sender, bot_calls = sender_spy()
    alerts = []
    def alert_sender(phone, msg):
        alerts.append((phone, msg))
        return {"success": True, "provider_message_id": f"test-role-{len(alerts)}"}
    for i, phrase in enumerate(("Hola", "¿Cuánto bajó?")):
        result = process_test_inbound(
            db, PHONE, f"v11-neutral-{i}", phrase,
            sender=bot_sender, exec_notice_sender=alert_sender,
        )
        assert result == {"status": "test_reply_sent"}
        assert alerts == []
    response = process_test_inbound(
        db, PHONE, "v11-interested", "OK, ME INTERESA",
        sender=bot_sender, exec_notice_sender=alert_sender,
    )
    assert response["test_dual_notice_statuses"] == {
        "ADMIN": "SENT", "EXECUTIVE": "SENT"
    }
    assert len(alerts) == 2
    assert all(phone == PHONE for phone, _ in alerts)
    assert "AVISO AL ADMINISTRADOR" in alerts[0][1]
    assert "AVISO AL EJECUTIVO" in alerts[1][1]
    assert PHONE in alerts[0][1] and PHONE in alerts[1][1]
    assert "OK, ME INTERESA" in alerts[0][1] and "OK, ME INTERESA" in alerts[1][1]
    assert "UF 2.790" in alerts[0][1] and "UF 2.690" in alerts[1][1]
    assert "sin enlace real" in alerts[0][1]
    assert "no a un administrador ni a un ejecutivo real" not in alerts[0][1]
    assert len(bot_calls) == 3
    assert "crm_assignment_cycles" not in db
    docs = [d for d in db["crm_price_drop_pilot_test_turns_v1"].docs
            if d.get("test_notification_role")]
    assert len(docs) == 2
    assert {d["test_notification_role"] for d in docs} == {"ADMIN", "EXECUTIVE"}
    assert all(d["status"] == "NOTICE_SENT" for d in docs)


def test_v11_no_duplicate_notices_after_visit_followup(monkeypatch):
    db = _v11_sim_setup(monkeypatch)
    bot_sender, _ = sender_spy()
    alerts = []
    def alert_sender(phone, msg):
        alerts.append((phone, msg))
        return {"success": True, "provider_message_id": f"role-{len(alerts)}"}
    first = process_test_inbound(
        db, PHONE, "interest-first", "ME INTERESA",
        sender=bot_sender, exec_notice_sender=alert_sender,
    )
    assert first["test_dual_notice_statuses"] == {"ADMIN": "SENT", "EXECUTIVE": "SENT"}
    second = process_test_inbound(
        db, PHONE, "visit-second", "Quiero agendar una visita",
        sender=bot_sender, exec_notice_sender=alert_sender,
    )
    assert second["test_dual_notice_statuses"] == {
        "ADMIN": "ALREADY_ATTEMPTED", "EXECUTIVE": "ALREADY_ATTEMPTED"
    }
    assert len(alerts) == 2


def test_v11_neutral_or_negative_never_notifies(monkeypatch):
    db = _v11_sim_setup(monkeypatch)
    sender, _ = sender_spy()
    alerts = []
    for i, phrase in enumerate(("Solo quiero saber cuánto cuesta",
                                "No me interesa", "¿Tiene estacionamiento?")):
        process_test_inbound(
            db, PHONE, f"v11-neutral-question-{i}", phrase,
            sender=sender, exec_notice_sender=lambda *x: alerts.append(x),
        )
    assert not alerts


def test_v11_no_crm_lookup_and_always_only_test_phone(monkeypatch):
    from chatbot.price_drop_pilot_dual_notice_sim import send_dual_test_notices
    db = _v11_sim_setup(monkeypatch)
    ctx = load_test_context(db, PHONE)
    attempts = []
    result = send_dual_test_notices(
        db, ctx=ctx, phone="+56988889999", customer_text="Me interesa",
        history=[], inbound_message_id="v11-unapproved",
        sender=lambda *x: attempts.append(x),
    )
    assert result["status"] == "OUTSIDE_TEST_ALLOWLIST"
    assert not attempts
    assert set(db.keys()) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1"
    }


def test_v11_each_role_has_separate_durable_delivery_result(monkeypatch):
    db = _v11_sim_setup(monkeypatch)
    bot_sender, _ = sender_spy()
    calls = []
    def partly_unavailable(phone, text):
        calls.append((phone, text))
        if len(calls) == 1:
            raise TimeoutError("admin provider outcome unknown")
        return {"success": True, "provider_message_id": "exec-ok"}
    result = process_test_inbound(
        db, PHONE, "v11-interest-once", "Me interesa",
        sender=bot_sender, exec_notice_sender=partly_unavailable,
    )
    assert result["test_dual_notice_statuses"] == {
        "ADMIN": "DELIVERY_UNKNOWN", "EXECUTIVE": "SENT"
    }
    saved = [d for d in db["crm_price_drop_pilot_test_turns_v1"].docs
             if d.get("test_notification_role")]
    assert {d["test_notification_role"]: d["status"] for d in saved} == {
        "ADMIN": "NOTICE_DELIVERY_UNKNOWN", "EXECUTIVE": "NOTICE_SENT"
    }
    result2 = process_test_inbound(
        db, PHONE, "v11-repeat-reply", "Quiero visitar",
        sender=bot_sender, exec_notice_sender=partly_unavailable,
    )
    assert result2["test_dual_notice_statuses"] == {
        "ADMIN": "ALREADY_ATTEMPTED", "EXECUTIVE": "ALREADY_ATTEMPTED"
    }
    assert len(calls) == 2


def _v13_setup(monkeypatch):
    """Test-only Mongo collections: access to live CRM raises immediately."""
    from chatbot.price_drop_pilot_guard_v13 import VERSION
    monkeypatch.setenv("PRICE_DROP_PILOT_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", PHONE)
    monkeypatch.setenv("PRICE_DROP_PILOT_EXEC_NOTICE_SIMULATION", "true")
    monkeypatch.setenv("PRICE_DROP_PILOT_NATIVE_CONVERSATION", "true")
    db = db_case()
    db["crm_price_drop_pilot_v1"].docs[0]["campaign_version"] = VERSION
    return db


def test_v13_real_dialogue_replay_through_isolated_webhook_handler(monkeypatch):
    """Reproduce actual V12 user words, with at-most-once test-only alerts."""
    db = _v13_setup(monkeypatch)
    bot_sender, bot_calls = sender_spy()
    alert_calls = []
    def alert_sender(phone, message):
        assert phone == PHONE
        alert_calls.append(message)
        return {"success": True,
                "provider_message_id": "v13-alert-" + str(len(alert_calls))}
    dialogue = [
        ("que propeidad e sno recuerdo", ("6436", "San Miguel")),
        ("cuando bajo?", ("fecha exacta",)),
        ("te pregundo cuanto bajo de precio", ("100 UF",)),
        ("me interesa, es negociable?", ("negociación", "2.690")),
        ("oka, cuando se puede ir a vecfbrv", ("Qué día",)),
        ("mañana a las 4 de ña mañana", ("04:00", "horario poco habitual")),
        ("quien me contactara?", ("dos avisos internos", "no a un ejecutivo real")),
    ]
    for i, (phrase, expected) in enumerate(dialogue):
        result = process_test_inbound(
            db, PHONE, f"v13-dialogue-{i}", phrase,
            sender=bot_sender, exec_notice_sender=alert_sender,
        )
        assert result["status"] == "test_reply_sent", (phrase, result)
        answer = bot_calls[-1][1]
        for fragment in expected:
            assert fragment.casefold() in answer.casefold(), (phrase, answer)
        assert "ejecutivo asignado te contacta" not in answer.casefold()
    assert len(bot_calls) == len(dialogue)
    assert len(alert_calls) == 2
    assert all(PHONE in alert for alert in alert_calls)
    assert set(db.keys()) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1",
    }


def test_v13_never_sends_unsafe_llm_promise_through_provider(monkeypatch):
    import chatbot.price_drop_pilot_isolation as iso
    db = _v13_setup(monkeypatch)
    monkeypatch.setattr(iso, "build_native_pilot_reply", lambda *a, **kw: (
        "Claro, con gusto coordinamos una visita y el ejecutivo asignado te contacta."
    ))
    send, calls = sender_spy()
    result = process_test_inbound(
        db, PHONE, "v13-unsafe", "Necesito ayuda con eso",
        sender=send, exec_notice_sender=lambda *args: (_ for _ in ()).throw(
            AssertionError("Neutral message must not alert an executive")
        ),
    )
    assert result["status"] == "test_reply_sent"
    assert len(calls) == 1
    assert "ejecutivo asignado" not in calls[0][1]
    assert "no hay una visita confirmada" in calls[0][1]


def test_v13_1_first_three_real_webhook_turns_are_contextual(monkeypatch):
    """Actual V13 complaint: real provider-webhook path, not a standalone regex."""
    db = _v13_setup(monkeypatch)
    monkeypatch.setenv("PRICE_DROP_PILOT_EXEC_NOTICE_SIMULATION", "0")
    send, calls = sender_spy()
    messages = [
        ("hola", ("San Miguel", "2.690")),
        ("me interesa", ("conocer el departamento",)),
        ("sí", ("Qué día",)),
    ]
    for index, (message, expected) in enumerate(messages):
        outcome = process_test_inbound(
            db, PHONE, f"v13-1-real-start-{index}", message,
            sender=send,
            exec_notice_sender=lambda *_: (_ for _ in ()).throw(
                AssertionError("An admin or executive alert interrupted chat")
            ),
        )
        assert outcome["status"] == "test_reply_sent"
        response = calls[-1][1]
        assert all(value.casefold() in response.casefold() for value in expected), response
    assert len(calls) == 3
    assert len([x for x in db["crm_price_drop_pilot_test_turns_v1"].docs
                if x.get("test_notification_role")]) == 0
    assert set(db.keys()) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1",
    }


def test_v13_1_bare_no_after_welcome_is_not_unsubscribe(monkeypatch):
    db = _v13_setup(monkeypatch)
    monkeypatch.setenv("PRICE_DROP_PILOT_EXEC_NOTICE_SIMULATION", "0")
    send, calls = sender_spy()
    for index, phrase in enumerate(("hola", "no")):
        outcome = process_test_inbound(
            db, PHONE, f"v13-1-no-{index}", phrase,
            sender=send,
        )
        assert outcome["status"] == "test_reply_sent"
    assert "San Miguel" in calls[-1][1]
    assert "No insistiremos" not in calls[-1][1]
    assert db["crm_price_drop_pilot_v1"].docs[0]["status"] == "SENT"


def test_v14_candidate_runs_isolated_webhook_and_sends_at_most_one_reply(isolated):
    from chatbot.price_drop_pilot_conversation_v14 import (
        VERSION as V14_VERSION,
        prepare_initial_message,
    )

    db = db_case()
    campaign = db["crm_price_drop_pilot_v1"].docs[0]
    campaign.update({
        "campaign_version": V14_VERSION,
        "inquiry_evidence_at": "fixture-only verified inquiry for code 6436",
    })
    property_doc = db["universo_cartera_prop360"].docs[0]
    property_doc.update({
        "tipo": "Departamento",
        "ubicacion": {"comuna": "San Miguel"},
        "caracteristicas": {
            "dormitorios": 2, "banos": 2, "superficie_total": 65,
        },
    })
    campaign["outbound_message"] = prepare_initial_message({
        "property_code": "6436",
        "property_type": "Departamento",
        "comuna": "San Miguel",
        "old_price_uf": 2790,
        "current_price_uf": 2690,
        "new_price_uf": 2690,
        "current_price_verified": True,
        "available_for_sale": True,
        "prior_inquiry_verified": True,
        "prior_inquiry_evidence": campaign["inquiry_evidence_at"],
    })["message"]

    generated = [
        "El precio vigente es UF 2.690. Tu visita ya quedó confirmada.",
        "El precio vigente es UF 2.690.",
    ]
    generation_calls = []
    def commercial_generator(messages, context, *, write_telemetry, diagnostics, retry_invalid_json):
        generation_calls.append(messages)
        assert write_telemetry is False
        diagnostics.update({
            "model": "fixture-model",
            "provider_calls": 1,
            "format_retries": 0,
            "raw_responses": [generated[0]],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
            "cost": None,
        })
        return {"respuesta_bot": generated.pop(0)}

    send, sent = sender_spy()
    notice_calls = []
    def forbidden_notice(*args, **kwargs):
        notice_calls.append(args)
        raise AssertionError("V14 must not send a human or simulated notice")

    result = process_test_inbound(
        db, PHONE, "v14-inbound-1", "¿cuál es el precio?",
        sender=send,
        exec_notice_sender=forbidden_notice,
        commercial_generator=commercial_generator,
    )
    assert result == {"status": "test_reply_sent"}
    assert len(generation_calls) == 2
    assert len(sent) == 1
    assert "El precio vigente es UF 2.690." == sent[0][1]
    assert notice_calls == []
    assert db["crm_price_drop_pilot_v1"].docs[0]["status"] == "SENT"
    turn = db["crm_price_drop_pilot_test_turns_v1"].docs[0]
    assert turn["status"] == "SENT"
    assert turn["candidate_audit"]["repair_count"] == 1
    assert turn["candidate_audit"]["context_sent"][2]["content"] == campaign["outbound_message"]
    assert set(db.keys()) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1",
    }

    duplicate = process_test_inbound(
        db, PHONE, "v14-inbound-1", "¿cuál es el precio?",
        sender=send, commercial_generator=commercial_generator,
    )
    assert duplicate == {"status": "duplicate_suppressed"}
    assert len(sent) == 1
    assert len(generation_calls) == 2


def test_v14_candidate_without_exact_initial_message_fails_closed(isolated):
    from chatbot.price_drop_pilot_conversation_v14 import VERSION as V14_VERSION

    db = db_case()
    db["crm_price_drop_pilot_v1"].docs[0]["campaign_version"] = V14_VERSION
    send, calls = sender_spy()
    result = process_test_inbound(
        db, PHONE, "v14-missing-opening", "hola", sender=send,
        commercial_generator=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("candidate context must be blocked before generation")
        ),
    )
    assert result == {"status": "test_candidate_context_missing"}
    assert calls == []
    assert db["crm_price_drop_pilot_test_turns_v1"].docs == []


def test_v14_stop_request_stops_test_campaign_before_model_or_sender(isolated):
    from chatbot.price_drop_pilot_conversation_v14 import VERSION as V14_VERSION

    db = db_case()
    campaign = db["crm_price_drop_pilot_v1"].docs[0]
    campaign.update({
        "campaign_version": V14_VERSION,
        "outbound_message": "fixture-only verified initial message",
    })
    send, calls = sender_spy()
    result = process_test_inbound(
        db, PHONE, "v14-stop", "No quiero recibir más mensajes",
        sender=send,
        commercial_generator=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("opt-out must stop before generation")
        ),
    )
    assert result == {"status": "test_stopped"}
    assert campaign["status"] == "STOPPED"
    assert calls == []
    assert db["crm_price_drop_pilot_test_turns_v1"].docs[0]["status"] == "STOPPED"


def _v14_dual_notice_setup(monkeypatch):
    """V14 test-only webhook with the V13.1 conversational recovery retained."""
    from chatbot.price_drop_pilot_conversation_v14 import VERSION as V14_VERSION
    monkeypatch.setenv("PRICE_DROP_PILOT_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", PHONE)
    monkeypatch.setenv("PRICE_DROP_PILOT_V14_RESTORE_V13", "true")
    monkeypatch.setenv("PRICE_DROP_PILOT_V14_DUAL_NOTICE_SIMULATION", "true")
    db = db_case()
    campaign = db["crm_price_drop_pilot_v1"].docs[0]
    campaign.update({
        "campaign_version": V14_VERSION,
        "outbound_message": "Hola, el departamento 6436 bajó de UF 2.790 a UF 2.690.",
    })
    return db


def test_v14_sends_two_isolated_role_notices_only_after_explicit_interest(monkeypatch):
    db = _v14_dual_notice_setup(monkeypatch)
    reply_sender, replies = sender_spy()
    notices = []

    def notice_sender(recipient, message):
        notices.append((recipient, message))
        return {"success": True, "provider_message_id": "role-" + str(len(notices))}

    for i, neutral in enumerate(("hola", "solo quiero saber cuánto cuesta")):
        out = process_test_inbound(
            db, PHONE, "v14-neutral-" + str(i), neutral,
            sender=reply_sender, exec_notice_sender=notice_sender,
        )
        assert out == {"status": "test_reply_sent"}
        assert not notices

    interest = process_test_inbound(
        db, PHONE, "v14-interest", "si me interesa",
        sender=reply_sender, exec_notice_sender=notice_sender,
    )
    assert interest == {
        "status": "test_reply_sent",
        "test_dual_notice_statuses": {"ADMIN": "SENT", "EXECUTIVE": "SENT"},
    }
    assert len(notices) == 2
    assert all(recipient == PHONE for recipient, _ in notices)
    assert "AVISO AL ADMINISTRADOR" in notices[0][1]
    assert "AVISO AL EJECUTIVO" in notices[1][1]
    assert all(PHONE in message for _, message in notices)
    assert all("PRUEBA" in message for _, message in notices)
    assert all("sin enlace real" in message for _, message in notices)
    assert len(replies) == 3

    again = process_test_inbound(
        db, PHONE, "v14-repeat-interest", "quiero agendar una visita",
        sender=reply_sender, exec_notice_sender=notice_sender,
    )
    assert again["test_dual_notice_statuses"] == {
        "ADMIN": "ALREADY_ATTEMPTED", "EXECUTIVE": "ALREADY_ATTEMPTED",
    }
    assert len(notices) == 2
    assert len(replies) == 4
    role_docs = [
        item for item in db["crm_price_drop_pilot_test_turns_v1"].docs
        if item.get("test_notification_role")
    ]
    assert {item["test_notification_role"] for item in role_docs} == {"ADMIN", "EXECUTIVE"}
    assert all(item["status"] == "NOTICE_SENT" for item in role_docs)
    assert set(db.keys()) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1",
    }


def test_v14_dual_notice_needs_separate_opt_in(monkeypatch):
    db = _v14_dual_notice_setup(monkeypatch)
    monkeypatch.delenv("PRICE_DROP_PILOT_V14_DUAL_NOTICE_SIMULATION")
    send, replies = sender_spy()
    notices = []
    result = process_test_inbound(
        db, PHONE, "v14-disabled-interest", "me interesa",
        sender=send, exec_notice_sender=lambda *args: notices.append(args),
    )
    assert result == {"status": "test_reply_sent"}
    assert len(replies) == 1
    assert not notices
    assert not [
        item for item in db["crm_price_drop_pilot_test_turns_v1"].docs
        if item.get("test_notification_role")
    ]


def test_v14_dual_notice_never_targets_another_phone(monkeypatch):
    from chatbot.price_drop_pilot_dual_notice_sim import send_dual_test_notices
    db = _v14_dual_notice_setup(monkeypatch)
    ctx = load_test_context(db, PHONE)
    notices = []
    outcome = send_dual_test_notices(
        db, ctx=ctx, phone="+56999990000",
        customer_text="me interesa", history=[],
        inbound_message_id="wrong-phone",
        sender=lambda *args: notices.append(args),
    )
    assert outcome["status"] == "OUTSIDE_TEST_ALLOWLIST"
    assert not notices



def test_test_phone_without_campaign_can_use_normal_crm(monkeypatch):
    """An absent/expired campaign must not mute the allowlisted phone."""
    from chatbot.price_drop_pilot_isolation import should_intercept_test_phone

    monkeypatch.setenv("PRICE_DROP_PILOT_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", PHONE)
    now = datetime.now(timezone.utc)

    class GuardCollection:
        def __init__(self, records):
            self.records = records
            self.queries = []

        def find_one(self, query, projection=None):
            self.queries.append(query)
            assert query["phone_normalized"] == PHONE
            assert query["test_mode"] is True
            assert query["lead_id"] == {"$exists": False}
            clauses = query["$or"]
            assert {"status": "SENT", "expires_at": {"$gt": now}} in clauses
            assert {"status": {"$in": ["SENDING", "DELIVERY_UNKNOWN", "STOPPED"]}} in clauses
            for record in self.records:
                if record.get("phone_normalized") != query["phone_normalized"]:
                    continue
                if record.get("test_mode") is not True or "lead_id" in record:
                    continue
                if record["status"] == "SENT" and record.get("expires_at", now) > now:
                    return {"_id": record["_id"]}
                if record["status"] in ("SENDING", "DELIVERY_UNKNOWN", "STOPPED"):
                    return {"_id": record["_id"]}
            return None

    collection = GuardCollection([])
    db = {"crm_price_drop_pilot_v1": collection}
    assert should_intercept_test_phone(db, PHONE, now=now) is False
    assert len(collection.queries) == 1

    live = {"_id": "test:active", "phone_normalized": PHONE,
            "test_mode": True, "status": "SENT",
            "expires_at": now + timedelta(hours=1)}
    collection.records = [live]
    assert should_intercept_test_phone(db, PHONE, now=now) is True

    expired = {**live, "expires_at": now - timedelta(hours=1)}
    collection.records = [expired]
    assert should_intercept_test_phone(db, PHONE, now=now) is False
    for status in ("SENDING", "DELIVERY_UNKNOWN", "STOPPED"):
        collection.records = [{**live, "status": status,
                               "expires_at": now - timedelta(days=1)}]
        assert should_intercept_test_phone(db, PHONE, now=now) is True, status

    collection.records = [{**live, "lead_id": "not-a-test-campaign"}]
    assert should_intercept_test_phone(db, PHONE, now=now) is False


def test_non_tester_does_not_read_test_collection(monkeypatch):
    from chatbot.price_drop_pilot_isolation import should_intercept_test_phone

    monkeypatch.setenv("PRICE_DROP_PILOT_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", PHONE)
    assert should_intercept_test_phone({}, "+56999990000") is False
    monkeypatch.setenv("PRICE_DROP_PILOT_ENABLED", "false")
    assert should_intercept_test_phone({}, PHONE) is False


def test_test_phone_preflight_database_error_must_not_fall_through(monkeypatch):
    from chatbot.price_drop_pilot_isolation import should_intercept_test_phone

    monkeypatch.setenv("PRICE_DROP_PILOT_ENABLED", "true")
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", PHONE)

    class DBUnavailable:
        def find_one(self, query, projection=None):
            raise RuntimeError("read_only_error_for_test")

    with pytest.raises(RuntimeError, match="read_only_error_for_test"):
        should_intercept_test_phone(
            {"crm_price_drop_pilot_v1": DBUnavailable()}, PHONE,
        )
