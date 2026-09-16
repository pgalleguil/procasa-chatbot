"""Regression tests for the only customer-facing provider admission barrier."""
import asyncio
from datetime import datetime, timedelta, timezone

import mongomock
import pytest

from chatbot import chatbot_queue as queue
from chatbot.final_response_barrier import admit_customer_response

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

# 82 permanent unsafe-writer variants covering the failures observed in shadow
# evaluation plus deliberately hostile variants.  They never hit a provider.
_RAW_UNSAFE_BASE = [
    "Sí, la propiedad está disponible.",
    "Tenemos disponibilidad mañana a las 10.",
    "Tu visita quedó agendada para mañana.",
    "El ejecutivo te llamará hoy.",
    "La comisión es un mes de arriendo más IVA.",
    "Ya registramos tus datos y tu interés.",
    "El propietario aceptará una oferta al contado.",
    "El banco aprobará tu crédito.",
    "La propiedad tiene 3 dormitorios y estacionamiento.",
    "Esta propiedad cuesta UF 5.200 y está en el rango de la zona.",
]
UNSAFE_FIXTURES = [
    {"id": f"unsafe-{number:02d}", "raw": f"{base} Caso {number}."}
    for number, base in enumerate((_RAW_UNSAFE_BASE * 9)[:82], 1)
]


@pytest.mark.parametrize("fixture", UNSAFE_FIXTURES, ids=lambda item: item["id"])
def test_all_82_unsafe_writer_outputs_are_repaired_or_fail_closed(fixture):
    admitted = admit_customer_response(
        candidate=fixture["raw"], customer_message="Quiero verla mañana.",
        lead={"prospecto": {"operacion": "Venta"}},
        property_context={"operation": "Venta", "property_code": "P-1"}, facts={}, source="fixture",
    )
    assert (admitted["approved"] and admitted["final_validation"]["valid"]) or not admitted["response"]


def _db():
    db = mongomock.MongoClient().barrier
    queue.ensure_queue_indexes(db)
    return db


@pytest.mark.parametrize("raw", _RAW_UNSAFE_BASE)
def test_production_queue_path_never_passes_raw_unsafe_text_to_sender(raw):
    db = _db()
    queue.create_inbound_job(db, inbound_provider_message_id=f"in-{abs(hash(raw))}",
                             phone="+56911112222", text="Quiero verla mañana", received_at=NOW)
    sent = []

    async def writer(_phone, _text, **_kwargs):
        return raw

    async def sender(_phone, text, **_kwargs):
        sent.append(text)
        return {"success": True, "provider_message_id": "fake-provider", "http_status": 200}

    result = asyncio.run(queue.process_one_batch(db, worker_id="barrier", llm=writer, sender=sender,
                                                  now=NOW + timedelta(seconds=30)))
    assert result["state"] == queue.ST_RESPONDED
    assert not sent or sent[0] != raw
    if sent:
        admitted = admit_customer_response(candidate=sent[0], customer_message="Quiero verla mañana",
                                            lead={"prospecto":{"operacion":"Venta"}},
                                            property_context={"operation":"Venta","property_code":"P-1"})
        assert admitted["final_validation"]["valid"]


def test_human_owner_blocks_even_a_repaired_response():
    admitted = admit_customer_response(candidate="Tu visita quedó agendada.", customer_message="Quiero verla",
        lead={"human_active": True, "prospecto":{"operacion":"Venta"}}, property_context={"operation":"Venta"})
    assert not admitted["approved"] and admitted["response"] == ""


def test_required_multi_intent_components_cannot_be_dropped():
    admitted = admit_customer_response(
        candidate="El precio informado es 5200.",
        customer_message="¿Está disponible, cuánto son los gastos comunes y puedo verla mañana?",
        lead={"prospecto":{"operacion":"Venta"}}, property_context={"operation":"Venta","property_code":"P-1"},
        facts={"precio_uf":5200,"gastos_comunes":90000}, source="writer",
    )
    assert admitted["approved"]
    assert "90000" in admitted["response"] and "disponibilidad" in admitted["response"].casefold()
    assert admitted["required_components_initially_dropped"] and not admitted["required_components_dropped"]


def test_required_multi_intent_repair_preserves_supplied_visit_preference():
    admitted = admit_customer_response(
        candidate="No tengo disponibilidad confirmada. Los gastos comunes son 90000. \u00bfQu\u00e9 d\u00eda te acomoda?",
        customer_message="\u00bfEst\u00e1 disponible, cu\u00e1nto son los gastos comunes y puedo verla ma\u00f1ana?",
        lead={"prospecto": {"operacion": "Venta"}},
        property_context={"operation": "Venta", "property_code": "P-1"},
        facts={"gastos_comunes": 90000}, source="writer",
    )
    assert admitted["approved"]
    assert "ma\u00f1ana" in admitted["response"].casefold()
    assert "¿qué día" not in admitted["response"].casefold()


def test_required_bedrooms_component_cannot_be_replaced_by_price_only():
    admitted = admit_customer_response(
        candidate="El precio informado es 5200.",
        customer_message="¿Cuánto cuesta y cuántos dormitorios tiene?",
        lead={"prospecto": {"operacion": "Venta"}},
        property_context={"operation": "Venta", "property_code": "P-1"},
        facts={"precio_uf": 5200, "dormitorios": 3}, source="writer",
    )
    assert admitted["approved"]
    assert "3 dormitorio" in admitted["response"]
    assert not admitted["required_components_dropped"]


def test_required_commission_component_cannot_be_replaced_by_property_price():
    admitted = admit_customer_response(
        candidate="El precio informado es 900000.",
        customer_message="¿Cuánto cobran de comisión?",
        lead={"actor_intent": "OWNER", "prospecto": {"operacion": "Arriendo"}},
        property_context={"operation": "Arriendo", "property_code": "P-1"},
        facts={"precio_clp": 900000}, source="writer",
    )
    assert admitted["approved"]
    assert "comisión" in admitted["response"].casefold()
    assert "900000" not in admitted["response"]
    assert not admitted["required_components_dropped"]


def test_barrier_repair_uses_durable_history_for_prior_visit_preference():
    admitted = admit_customer_response(
        candidate="Perfecto, registré tu preferencia para el viernes. ¿Te acomoda otro horario?",
        customer_message="Finalmente será al contado",
        lead={
            "prospecto": {
                "operacion": "Venta",
                "visit_preference": {"text": "viernes", "property_id": "P-1"},
            },
            "messages": [
                {"role": "user", "content": "También quisiera verla el viernes"},
                {"role": "assistant", "content": "La disponibilidad debe confirmarse antes de agendar."},
            ],
        },
        property_context={"operation": "Venta", "property_code": "P-1"},
        facts={"precio_uf": 5200}, source="writer",
    )
    assert admitted["approved"]
    assert "viernes" in admitted["response"].casefold()
    assert "enlace o código" not in admitted["response"].casefold()


def test_rag_result_is_preserved_when_required_fact_repair_runs():
    admitted = admit_customer_response(
        candidate="Hay disponibilidad para esa alternativa.",
        customer_message="Busco departamento para arrendar en Providencia, 2 dormitorios, hasta 800 mil",
        lead={"prospecto": {"operacion": "Arriendo"}},
        property_context={"operation": "Arriendo"},
        facts={"rag_result_count": 1, "dormitorios": 2, "precio_clp": 800000}, source="writer",
    )
    assert admitted["approved"]
    assert "alternativa" in admitted["response"].casefold()
    assert "2 dormitorio" in admitted["response"]
    assert not admitted["required_components_dropped"]


def test_new_inbound_during_final_validation_is_superseded_before_sender(monkeypatch):
    db = _db()
    queue.create_inbound_job(db, inbound_provider_message_id="barrier-stale-1", phone="+56911112222",
                             text="primero", received_at=NOW)
    from chatbot import final_response_barrier
    original = final_response_barrier.admit_customer_response
    def validate_then_receive(*args, **kwargs):
        queue.create_inbound_job(db, inbound_provider_message_id="barrier-stale-2", phone="+56911112222",
                                 text="segundo", received_at=NOW + timedelta(seconds=31))
        return original(*args, **kwargs)
    monkeypatch.setattr(final_response_barrier, "admit_customer_response", validate_then_receive)
    sent=[]
    async def writer(_phone, _text, **_kwargs): return "Respuesta segura"
    async def sender(_phone, text, **_kwargs): sent.append(text); return {"success":True,"provider_message_id":"unexpected","http_status":200}
    result=asyncio.run(queue.process_one_batch(db,worker_id="stale",llm=writer,sender=sender,now=NOW+timedelta(seconds=30)))
    assert result["state"] == queue.ST_BATCHING and sent == []


def test_human_takeover_after_final_validation_blocks_sender(monkeypatch):
    db=_db(); queue.create_inbound_job(db,inbound_provider_message_id="barrier-human",phone="+56911112222",text="primero",received_at=NOW)
    from chatbot import final_response_barrier
    original=final_response_barrier.admit_customer_response; state={"taken":False}
    def validate_then_handoff(*args,**kwargs):
        result=original(*args,**kwargs); state["taken"]=True; return result
    monkeypatch.setattr(final_response_barrier,"admit_customer_response",validate_then_handoff)
    monkeypatch.setattr(queue,"_human_takeover_after_batch_start",lambda *_args,**_kwargs: state["taken"])
    sent=[]
    async def writer(_phone,_text,**_kwargs): return "Respuesta segura"
    async def sender(_phone,text,**_kwargs): sent.append(text); return {"success":True,"provider_message_id":"unexpected","http_status":200}
    result=asyncio.run(queue.process_one_batch(db,worker_id="human",llm=writer,sender=sender,now=NOW+timedelta(seconds=30)))
    assert result["state"] == queue.ST_RESPONDED and sent == []
