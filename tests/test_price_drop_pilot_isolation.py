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
        "property_code": "6436",
        "old_price_uf": 2790,
        "new_price_uf": 2690,
        "property_facts": {"banos": 2},
    }
    hist = [{"customer_text": "oka me interesa",
             "bot_text": "Si quieres conocerlo, dime qué día u horario te acomodaría para una posible visita."}]
    for msg in ("el proximo mes", "la próxima semana", "por la tarde"):
        answer = build_test_reply(msg, ctx, hist, llm=lambda *_: (_ for _ in ()).throw(AssertionError("should not ask model")))
        assert "6436" in answer
        assert "no se ha agendado" in answer
        assert "¿Tienes un día u horario" in answer


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
    ("¿Puedo visitar el próximo mes?", "¿Qué horario"),
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
        ("oka me interesa", "más información o prefieres coordinar"),
        ("quiero visitarla", "¿Qué día"),
        ("el proximo mes", "no se ha agendado"),
        ("el martes", "¿Tienes un día"),
        ("por la tarde", "no se ha agendado"),
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


def test_v3_customer_message_is_natural_and_contains_no_unverified_history():
    """Only factual property and price are included; no invented customer details."""
    from scripts import price_drop_pilot_test as sender_script
    source = Path(__file__).resolve().parents[1].joinpath(
        "scripts/price_drop_pilot_test.py"
    ).read_text()
    assert sender_script.TEST_ID.endswith("-v3")
    assert '"campaign_version": "price_drop_pilot_v3"' in source
    # Text segments in the message block must not claim facts about the lead.
    segment = source.split("    message = (", 1)[1].split("    if not send:", 1)[0]
    for excluded in ("PRUEBA INTERNA", "SEGUNDA PRUEBA", "habías consultado",
                     "me comentaste", "[Nombre]", "nombre del cliente",
                     "le enviamos a tu ejecutiv"):
        assert excluded not in segment
    for required in ("¡Hola! ¿Cómo estás?", "PROCASA", "San Miguel",
                     "UF 2.790", "UF 2.690", "procasa.cl/6436",
                     "¿Te gustaría saber más o ir a verlo?"):
        assert required in segment
