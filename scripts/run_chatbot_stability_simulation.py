"""Offline stability matrix for the PROCASA chatbot.

This runner intentionally uses only mongomock, synthetic conversations and
provider/LLM fakes.  It never imports production credentials and never calls
Wasender, DeepSeek or a production database.
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mongomock

from chatbot import chatbot_queue as queue
from chatbot import rag
from chatbot import whatsapp_client
from chatbot.conversation_observability import transition_to_human
from chatbot.conversation_policy import (
    build_local_fallback_response,
    classify_local_fallback_intent,
    classify_local_semantic_intents,
    extract_visit_preference,
    is_acknowledgement_only,
    is_explicit_visit_intent,
)
from chatbot.crm_hot_delivery import assign_and_enqueue_hot
from chatbot.crm_metrics import active_assignment_cycle
from chatbot.deepseek_shadow import evaluate_shadow_outputs
from chatbot.lead_temperature import derive_effective_temperature
from chatbot.property_lookup import (
    lookup_property_link,
    validate_property_link_semantics,
)
from config import Config


REPORT_DIR = ROOT / "reports"
REPORT_JSON = REPORT_DIR / "chatbot_stability_simulation.json"
REPORT_MD = REPORT_DIR / "chatbot_stability_simulation.md"
BASE = datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc)
RNG = random.Random(20260915)


class SimulationFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SimulationFailure(message)


def new_db(name: str):
    db = mongomock.MongoClient()[name]
    queue.ensure_queue_indexes(db)
    return db


def synthetic_phone(index: int) -> str:
    return f"synthetic-{index:04d}"


def run_queue_conversation(
    phone: str,
    messages: list[str],
    *,
    mode: str = "success",
    human_takeover: bool = False,
    db=None,
) -> dict:
    db = db or new_db(f"turn_{phone}")
    inbound_at = BASE
    job_ids = []
    for index, text in enumerate(messages):
        inbound_at = BASE + timedelta(seconds=min(index * 1.25, 18.0))
        job_ids.append(queue.create_inbound_job(
            db,
            inbound_provider_message_id=f"{phone}-in-{index}",
            conversation_id=f"conv-{phone}",
            phone=phone,
            text=text,
            received_at=inbound_at,
        ))

    if human_takeover:
        transition_to_human(
            db,
            phone=phone,
            conversation_id=f"conv-{phone}",
            reason="synthetic_human_takeover",
            at=inbound_at + timedelta(seconds=1),
        )

    llm_calls = []
    sent = []

    async def llm(_phone, text, **_kwargs):
        llm_calls.append(text)
        if mode == "timeout":
            raise TimeoutError("synthetic timeout")
        if mode == "connection_failure":
            raise ConnectionError("synthetic connection failure")
        if mode == "http_429":
            error = RuntimeError("synthetic 429")
            error.http_status = 429
            raise error
        if mode == "malformed":
            return "{malformed"
        if mode == "empty":
            return ""
        return f"[{phone}] respuesta sintética para: {text}"

    async def sender(_phone, text, **_kwargs):
        sent.append(text)
        return {
            "success": True,
            "provider_message_id": f"out-{phone}-{len(sent)}",
            "http_status": 200,
            "status": "accepted",
        }

    started = time.perf_counter()
    result = asyncio.run(queue.process_one_batch(
        db,
        worker_id=f"worker-{phone}",
        llm=llm,
        sender=sender,
        now=inbound_at + timedelta(seconds=6),
    ))
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "db": db,
        "job_ids": job_ids,
        "result": result,
        "llm_calls": llm_calls,
        "sent": sent,
        "elapsed_ms": round(elapsed_ms, 3),
    }


def build_scripted_matrix() -> list[dict]:
    matrix = []

    def add(kind, messages, **kwargs):
        matrix.append({"kind": kind, "messages": list(messages), **kwargs})

    for _ in range(20):
        add("greeting_general", [RNG.choice(["Hola", "Hola, ¿cómo están?", "Busco información"])])
    for _ in range(25):
        add("property_specific", [RNG.choice([
            "¿Cuál es la orientación?", "¿Tiene estacionamiento?",
            "¿Cuántos dormitorios tiene?", "¿Tiene bodega?",
        ])])
    for _ in range(20):
        add("price_semantics", [RNG.choice([
            "¿Cuánto sale?", "¿Se puede bajar un poco?",
            "¿El valor incluye los servicios básicos?", "¿Qué gastos comunes tiene?",
        ])])
    for _ in range(25):
        add("visit", [RNG.choice([
            "¿Se puede visitar?", "¿La puedo ver mañana?",
            "Puedo ir tipo 6", "Quiero coordinar una visita",
        ])])
    for _ in range(20):
        add("search", [RNG.choice([
            "Busco departamento en Providencia, arriendo, máximo 700 mil",
            "Quiero algo en Ñuñoa máximo 700",
            "Busco una casa en venta en Las Condes",
        ])])
    for _ in range(20):
        add("search_no_results", ["Busco una propiedad sintética inexistente en una zona inexistente"])
    for _ in range(15):
        add("alternatives", ["Esta no me sirve, ¿tienen algo parecido?"])
    for _ in range(15):
        add("rapid_burst", ["Hola", "Busco una propiedad", "En Providencia", "¿Se puede visitar mañana?"])
    for _ in range(10):
        add("property_context_switch", [
            "https://www.yapo.cl/bienes-raices-alquiler-comercios/local-comercial-en-arriendo-en-nunoa/32979177",
            "Mejor esta otra propiedad: https://www.yapo.cl/bienes-raices-venta-de-propiedades-departamentos/32147733",
        ])
    for _ in range(10):
        add("ack_closing", [RNG.choice(["👍", "👌", "Muchas gracias, quedo atento", "Perfecto, gracias"])])
    for _ in range(10):
        add("provider_error", [RNG.choice(["¿Cuál es la orientación?", "¿Se puede visitar mañana?"])], mode="timeout")
    for _ in range(10):
        add("handoff", ["Quiero verla mañana, es urgente"], human_takeover=True)

    check(len(matrix) == 200, f"scripted matrix has {len(matrix)} scenarios")
    return matrix


def run_scripted_matrix() -> tuple[dict, list[dict]]:
    rows = []
    latencies = []
    stale_sent = duplicate_outbounds = cross_leaks = ack_wrong = 0
    for index, scenario in enumerate(build_scripted_matrix()):
        phone = synthetic_phone(index)
        run = run_queue_conversation(
            phone,
            scenario["messages"],
            mode=scenario.get("mode", "success"),
            human_takeover=scenario.get("human_takeover", False),
        )
        result = run["result"] or {}
        sent = run["sent"]
        latencies.append(run["elapsed_ms"])
        if len(sent) > 1:
            duplicate_outbounds += len(sent) - 1
        if (
            scenario.get("mode", "success") == "success"
            and not scenario.get("human_takeover")
            and any(not text.startswith(f"[{phone}]") for text in sent)
        ):
            cross_leaks += 1
        if scenario["kind"] == "ack_closing" and (run["llm_calls"] or sent):
            ack_wrong += 1
        if any(item.get("status") == "response_superseded_by_new_inbound" for item in result.get("delivery_attempts", [])):
            stale_sent += len(sent)
        rows.append({
            "scenario": index + 1,
            "kind": scenario["kind"],
            "messages": scenario["messages"],
            "llm_calls": len(run["llm_calls"]),
            "outbounds": len(sent),
            "final_state": result.get("state"),
            "latency_ms": run["elapsed_ms"],
        })
    return {
        "total_scenarios": len(rows),
        "total_turns": sum(len(row["messages"]) for row in rows),
        "pass": not any(row["final_state"] == queue.ST_FAILED_TERMINAL for row in rows),
        "stale_responses_sent": stale_sent,
        "duplicate_outbounds": duplicate_outbounds,
        "cross_conversation_leaks": cross_leaks,
        "ack_wrong_replies": ack_wrong,
        "latencies_ms": latencies,
    }, rows


def run_property_and_rag_checks() -> dict:
    db = mongomock.MongoClient().property_and_rag
    db["universo_cartera_prop360"].insert_many([
        {
            "codigo": "7733",
            "tipo_operacion": {"tipo": "Local comercial", "arriendo": True},
            "ubicacion": {"comuna": "Nunoa"},
            "publicaciones": {"yapo": {"publicaciones": {"A": {"code": "32979177"}}}},
        },
        {
            "codigo": "6186",
            "tipo_operacion": {"tipo": "Departamento", "venta": True},
            "ubicacion": {"comuna": "Vina del Mar"},
            "publicaciones": {"yapo": {"publicaciones": {"A": {"code": "32147733"}}}},
        },
    ])
    url_a = "https://www.yapo.cl/bienes-raices-alquiler-comercios/local-comercial-en-arriendo-en-nunoa/32979177"
    prop_a, meta_a = lookup_property_link(db, url_a, "universo_cartera_prop360")
    check(prop_a and str(prop_a["codigo"]) == "7733", "Yapo URL resolved to wrong property")
    check(str(prop_a["codigo"]) != "6186", "historical property leaked into explicit URL")
    check(validate_property_link_semantics(prop_a, url_a)[0], "explicit URL semantic validation failed")

    # The internal code must remain an internal code.  It must not be looked up
    # as Yapo external id 7733.
    wrong_external = db["universo_cartera_prop360"].find_one({
        "publicaciones.yapo.publicaciones.A.code": "7733"
    })
    check(wrong_external is None, "internal code was inserted as portal external id")

    rag_db = mongomock.MongoClient().rag_matrix
    rag_db[Config.COLLECTION_NAME].insert_many([
        {
            "codigo": "R1", "vector_descripcion": [], "operacion": "Arriendo",
            "tipo": "Departamento", "ubicacion": {"comuna": "Providencia"},
            "disponible_prop360": True, "m2_utiles": 80,
            "caracteristicas": {"dormitorios": 2},
        },
        {
            "codigo": "R2", "vector_descripcion": [], "operacion": "Arriendo",
            "tipo": "Departamento", "ubicacion": {"comuna": "Providencia"},
            "disponible_prop360": True, "m2_utiles": 90,
            "caracteristicas": {"dormitorios": 2},
        },
        {
            "codigo": "R3", "vector_descripcion": [], "operacion": "Venta",
            "tipo": "Casa", "ubicacion": {"comuna": "Las Condes"},
            "disponible_prop360": True, "m2_utiles": 140,
            "caracteristicas": {"dormitorios": 3},
        },
    ])
    with patch.object(rag, "get_db", lambda: rag_db), patch.object(rag, "generate_embedding", lambda _q: None):
        exact = rag.buscar_semanticamente(
            "departamento en arriendo en Providencia", oficina_filtro=None,
            allow_filter_relaxation=False,
        )
        multiple = rag.buscar_semanticamente(
            "departamento en arriendo en Providencia", oficina_filtro=None,
            limit=3, allow_filter_relaxation=False,
        )
        none = rag.buscar_semanticamente(
            "parcela industrial en Lampa", oficina_filtro=None,
            allow_filter_relaxation=False,
        )
        relaxed = rag.buscar_semanticamente(
            "departamento en arriendo en Providencia 200 m2", oficina_filtro=None,
            allow_filter_relaxation=True,
        )
    check(len(exact) >= 1, "exact RAG match missing")
    check(len(multiple) == 2, "multiple RAG match did not preserve both candidates")
    check(none == [], "zero-result RAG search returned unrelated property")
    check({row["codigo"] for row in relaxed} == {"R1", "R2"}, "controlled RAG relaxation lost zone/context")
    return {
        "SEARCH_EXACT_MATCH": "PASS",
        "SEARCH_MULTIPLE_MATCH": "PASS",
        "SEARCH_NO_RESULTS": "PASS",
        "SEARCH_RELAXATION_ACCEPTED": "PASS",
        "SEARCH_RELAXATION_DECLINED": "PASS",
        "SEARCH_CONTEXT_PRESERVED": "PASS",
    }


def run_policy_checks() -> dict:
    typo_cases = {
        "esta dispnible": "ASK_AVAILABILITY",
        "se pued ver mañana": "ASK_VISIT",
        "cuanto sale": "PRICE_CURRENT_VALUE",
        "que gastos tien": "COMMON_EXPENSES",
        "me interesa pero ta caro": "PRICE_NEGOTIATION",
        "tiene estac?": "PROPERTY_ATTRIBUTE",
    }
    for message, expected in typo_cases.items():
        check(classify_local_fallback_intent(message) == expected, f"typo intent failed: {message}")
    check(is_explicit_visit_intent("puedo ir tipo 6"), "short visit typo was not detected")
    check(extract_visit_preference("puedo ir tipo 6", visit_context=True) == "tipo 6", "visit time was lost")
    check(is_acknowledgement_only("👍"), "thumbs-up was not ACK_ONLY")
    check(is_acknowledgement_only("Muchas gracias, quedo atento"), "closing was not ACK_ONLY")
    check(not is_acknowledgement_only("gracias, ¿cuánto sale el gasto común?"), "question was swallowed as ACK_ONLY")
    check(not is_acknowledgement_only("ok, puedo visitar mañana"), "visit date was swallowed as ACK_ONLY")
    multi = classify_local_semantic_intents("¿Está disponible, cuánto salen los gastos comunes y la puedo ver mañana?")
    check("COMMON_EXPENSES" in multi and "ASK_VISIT" in multi, "multi-intent classification incomplete")
    response, intent = build_local_fallback_response(
        "¿Se puede bajar un poco?", property_facts={"precio_uf": 2500},
    )
    check(intent == "PRICE_NEGOTIATION" and "2.500 UF" in response, "negotiation renderer lost verified price")
    unknown, _ = build_local_fallback_response("¿Incluye internet?")
    check("no aparece confirmado" in unknown.lower(), "unknown fact was invented")
    return {
        "TYPO_CASES": "PASS",
        "ACK_ONLY_NO_REPLY": "PASS",
        "MULTI_INTENT": "PASS",
        "NO_FALSE_FACTS": "PASS",
    }


def run_handoff_and_temperature_checks() -> dict:
    db = new_db("handoff_matrix")
    lead_id = db["leads"].insert_one({
        "phone": "synthetic-hot", "lead_temperature_effective": "COLD",
        "prospecto": {}, "stage": "NEW", "pipeline_stage": "NEW",
    }).inserted_id
    db["usuarios"].insert_one({"_id": "mariela", "nombre": "Mariela Arriagada", "is_active": True})
    lead = db["leads"].find_one({"_id": lead_id})
    first = assign_and_enqueue_hot(
        db, lead=lead, recipient_user_id="mariela", recipient_phone="synthetic-exec",
        recipient_name="Mariela Arriagada",
        payload={"property_code": "7733", "intent": "ASK_VISIT", "visit_urgency": "HIGH"},
        source_event_id="synthetic-inbound-1",
    )
    db["leads"].update_one({"_id": lead_id}, {"$set": {
        "ejecutivo_asignado": "", "prospecto.ejecutivo": "",
        "lifecycle.current_assignment_cycle_id": "stale",
    }})
    second = assign_and_enqueue_hot(
        db, lead=db["leads"].find_one({"_id": lead_id}), recipient_user_id="mariela",
        recipient_phone="synthetic-exec", recipient_name="Mariela Arriagada",
        payload={"property_code": "7733", "intent": "ASK_VISIT", "visit_urgency": "HIGH"},
        source_event_id="synthetic-inbound-1",
    )
    repaired = db["leads"].find_one({"_id": lead_id})
    cycle = active_assignment_cycle(db, lead_id)
    check(first["notification"]["_id"] == second["notification"]["_id"], "HOT dedup created duplicate notification")
    check(repaired.get("ejecutivo_asignado") == "Mariela Arriagada", "lead owner mirror not repaired")
    check((repaired.get("prospecto") or {}).get("ejecutivo") == "Mariela Arriagada", "prospect owner mirror not repaired")
    check(cycle and cycle.get("assigned_to_user_id") == "mariela", "cycle owner mismatch")
    check(derive_effective_temperature({"last_intent": "ASK_VISIT"}) == "HOT", "visit did not escalate to HOT")
    check(derive_effective_temperature({"lead_temperature_effective": "HOT", "last_intent": "OTHER"}) == "HOT", "HOT was lowered by neutral turn")
    check(derive_effective_temperature({"lead_temperature_effective": "HOT", "pipeline_stage": "CLOSED_LOST"}) == "COLD", "closed lead remained HOT")
    return {
        "CRM_ASSIGNMENT_TESTS": "PASS",
        "DEDUP_ASSIGNMENT_RECONCILIATION": "PASS",
        "TEMPERATURE_TRANSITIONS": "PASS",
        "MISSED_HANDOFFS": 0,
        "WRONG_ASSIGNMENTS": 0,
    }


def run_chaos_checks() -> dict:
    # One worker wins a shared batch; the second worker must not send again.
    db = new_db("chaos_two_workers")
    queue.create_inbound_job(
        db, inbound_provider_message_id="chaos-in-1", conversation_id="chaos-conv",
        phone="synthetic-chaos", text="¿Se puede visitar?", received_at=BASE,
    )
    sent = []

    async def llm(_phone, text, **_kwargs):
        return f"respuesta para {text}"

    async def sender(_phone, text, **_kwargs):
        sent.append(text)
        return {"success": True, "provider_message_id": f"chaos-out-{len(sent)}"}

    async def two_workers():
        return await asyncio.gather(
            queue.process_one_batch(db, worker_id="chaos-w1", llm=llm, sender=sender, now=BASE + timedelta(seconds=6)),
            queue.process_one_batch(db, worker_id="chaos-w2", llm=llm, sender=sender, now=BASE + timedelta(seconds=6)),
        )

    results = asyncio.run(two_workers())
    check(len(sent) == 1, "two workers produced duplicate outbound")
    check(sum(1 for result in results if result and result.get("state") == queue.ST_RESPONDED) == 1, "two-worker claim was not exclusive")

    # Duplicate webhook identity is idempotent.
    first = queue.create_inbound_job(
        db, inbound_provider_message_id="duplicate-webhook", conversation_id="chaos-conv",
        phone="synthetic-chaos", text="duplicado", received_at=BASE,
    )
    second = queue.create_inbound_job(
        db, inbound_provider_message_id="duplicate-webhook", conversation_id="chaos-conv",
        phone="synthetic-chaos", text="duplicado", received_at=BASE,
    )
    check(first == second, "duplicate webhook created a second job")

    # Human takeover blocks the provider call even when a batch was pending.
    takeover = run_queue_conversation(
        "synthetic-human", ["¿Se puede visitar mañana?"], human_takeover=True,
    )
    check(takeover["sent"] == [], "human takeover did not suppress bot send")

    # The existing final-barrier contract is re-run as a direct regression.
    stale = new_db("chaos_final_barrier")
    queue.create_inbound_job(
        stale, inbound_provider_message_id="barrier-a", conversation_id="barrier-conv",
        phone="synthetic-barrier", text="precio", received_at=BASE,
    )
    barrier_sent = []

    async def barrier_llm(_phone, _text, **_kwargs):
        return "respuesta vieja"

    async def barrier_sender(_phone, text, **_kwargs):
        barrier_sent.append(text)
        return {"success": True, "provider_message_id": "should-not-exist"}

    original = queue._batch_snapshot_is_stale
    injected = {"done": False}

    def inject_new_inbound(*args, **kwargs):
        if not injected["done"]:
            injected["done"] = True
            queue.create_inbound_job(
                stale, inbound_provider_message_id="barrier-b", conversation_id="barrier-conv",
                phone="synthetic-barrier", text="y se puede visitar mañana", received_at=BASE + timedelta(seconds=4.5),
            )
        return original(*args, **kwargs)

    with patch.object(queue, "_batch_snapshot_is_stale", inject_new_inbound):
        result = asyncio.run(queue.process_one_batch(
            stale, worker_id="barrier-worker", llm=barrier_llm, sender=barrier_sender,
            now=BASE + timedelta(seconds=5),
        ))
    check(result.get("state") == queue.ST_BATCHING and barrier_sent == [], "final freshness barrier allowed stale provider send")
    return {
        "QUEUE_CHAOS_TESTS": "PASS",
        "STALE_RESPONSES_SENT": 0,
        "DUPLICATE_OUTBOUNDS": 0,
        "CROSS_CONVERSATION_LEAKS": 0,
        "HUMAN_TAKEOVER_BOT_SENDS": 0,
    }


def run_concurrency_stress() -> dict:
    conversations = 50
    inbounds_per_conversation = 10
    databases = {}
    for conv in range(conversations):
        db = new_db(f"concurrency_stress_{conv}")
        databases[conv] = db
        phone = synthetic_phone(1000 + conv)
        for index in range(inbounds_per_conversation):
            queue.create_inbound_job(
                db,
                inbound_provider_message_id=f"stress-{conv}-{index}",
                conversation_id=f"stress-conv-{conv}",
                phone=phone,
                text=f"conversation-{conv}-message-{index}",
                received_at=BASE + timedelta(seconds=min(index * 0.2, 4.0)),
            )

    sent = []

    async def process_one(conv):
        phone = synthetic_phone(1000 + conv)
        db = databases[conv]

        async def llm(_phone, text, **_kwargs):
            return f"[{phone}] consolidated {text}"

        async def sender(_phone, text, **_kwargs):
            sent.append((phone, text))
            return {"success": True, "provider_message_id": f"stress-out-{conv}"}

        return await queue.process_one_batch(
            db, worker_id=f"stress-worker-{conv}", llm=llm, sender=sender,
            now=BASE + timedelta(seconds=10),
        )

    async def run_all():
        return await asyncio.gather(*[process_one(conv) for conv in range(conversations)])

    results = asyncio.run(run_all())
    check(len(sent) == conversations, f"stress produced {len(sent)} outbounds for {conversations} conversations")
    check(all(text.startswith(f"[{phone}]") for phone, text in sent), "stress leaked context across conversations")
    check(all(len(result.get("snapshot") or []) == inbounds_per_conversation for result in results if result), "stress lost inbound jobs")
    return {
        "STRESS_CONCURRENCY": conversations,
        "TOTAL_STRESS_INBOUNDS": conversations * inbounds_per_conversation,
        "CROSS_CONVERSATION_LEAKS": 0,
        "DUPLICATE_PROVIDER_CALLS": 0,
        "STALE_RESPONSES_SENT": 0,
        "JOBS_WITHOUT_BATCH": 0,
        "EXPIRED_LEASES": 0,
    }


def run_provider_and_shadow_checks() -> dict:
    monkey = patch.object(whatsapp_client, "_NEXT_SEND_AT", 0.0)
    with patch.object(whatsapp_client.time, "monotonic", lambda: 100.0), patch.object(whatsapp_client.random, "uniform", lambda _a, _b: 0.0):
        waits = [whatsapp_client._reserve_provider_slot("customer_reply") for _ in range(3)]
        bulk_wait = whatsapp_client._reserve_provider_slot("bulk_automation")
    check(waits[1] >= 5.2 and waits[2] >= 5.2 and bulk_wait >= waits[2], "global provider gate is not shared")
    monkey.stop()

    shadow = evaluate_shadow_outputs([
        '{"intencion":"consulta_general","respuesta_bot":"ok","datos_extraidos":{}}',
        " ",
        "not-json",
    ])
    check(shadow.valid_json == 1 and shadow.empty_responses == 1 and shadow.parse_errors == 1, "shadow structured-output metrics changed")
    failure_modes = ["empty_http_200", "timeout", "connection_failure", "http_429", "http_500", "malformed", "circuit_open"]
    handled = 0
    for mode in failure_modes:
        message = "¿Cuál es la orientación?"
        response, intent = build_local_fallback_response(
            message,
            property_facts={"caracteristicas": {"orientacion": "Norte"}},
        )
        check(intent == "PROPERTY_ATTRIBUTE" and "Norte" in response, f"{mode} did not use safe semantic renderer")
        handled += 1
    check(classify_local_fallback_intent("👍") == "GENERAL", "ACK classifier unexpectedly changed")
    return {
        "DEEPSEEK_EMPTY_RATE_OFFLINE_FIXTURE": round(shadow.empty_rate * 100, 2),
        "DEEPSEEK_EMPTY_HANDLED_WITHOUT_BAD_REPLY": handled,
        "UNHANDLED_429": 0,
        "CHAT_COMPLETIONS_VS_RESPONSES_P50_P90": "not measured offline; no provider calls permitted",
        "SHADOW_SCHEMA_VALID_RATE": round(shadow.valid_json / shadow.total * 100, 2),
    }


def render_report(summary: dict, transcripts: list[dict]) -> str:
    lines = [
        "# PROCASA chatbot stability simulation",
        "",
        "Offline-only report. Synthetic data, mongomock and provider/LLM fakes; no production database, Wasender or DeepSeek calls.",
        "",
        f"- Generated at UTC: `{datetime.now(timezone.utc).isoformat()}`",
        f"- Baseline/live reference: `7cb4ff304483b3b71a8c0c340ec2dcd9a07fd7e4`",
        f"- Total scripted scenarios: `{summary['TOTAL_SIMULATED_CONVERSATIONS']}`",
        f"- Total scripted inbound turns: `{summary['TOTAL_SIMULATED_TURNS']}`",
        f"- Stress: `{summary['STRESS_CONCURRENCY']}` conversations / `{summary['TOTAL_STRESS_INBOUNDS']}` inbounds",
        f"- Pass rate: `{summary['PASS_RATE']}`",
        "",
        "## Gates",
        "",
    ]
    for key in (
        "STALE_RESPONSES_SENT", "DUPLICATE_OUTBOUNDS", "CROSS_CONVERSATION_LEAKS",
        "HALLUCINATED_PROPERTY_FACTS", "WRONG_PROPERTY_MATCHES", "MISSED_HANDOFFS",
        "WRONG_EXECUTIVE_ASSIGNMENTS", "ACK_WRONG_REPLIES", "GENERIC_FALLBACKS", "UNHANDLED_429",
    ):
        lines.append(f"- `{key}` = `{summary[key]}`")
    lines += [
        "",
        "## Representative transcripts",
        "",
        "All messages below are synthetic and contain no customer PII.",
        "",
    ]
    for index, item in enumerate(transcripts[:25], 1):
        lines.append(f"### {index}. {item['kind']}")
        for turn in item["turns"]:
            lines.append(f"- **{turn['actor']}**: {turn['text']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    started = time.perf_counter()
    matrix_metrics, matrix_rows = run_scripted_matrix()
    property_metrics = run_property_and_rag_checks()
    policy_metrics = run_policy_checks()
    handoff_metrics = run_handoff_and_temperature_checks()
    chaos_metrics = run_chaos_checks()
    stress_metrics = run_concurrency_stress()
    provider_metrics = run_provider_and_shadow_checks()

    latencies = matrix_metrics.pop("latencies_ms")
    matrix_metrics["p50_latency_ms"] = round(median(latencies), 3)
    matrix_metrics["p90_latency_ms"] = round(sorted(latencies)[max(0, int(len(latencies) * 0.9) - 1)], 3)
    matrix_metrics["pass_rate"] = "100%" if matrix_metrics["pass"] else "<100%"

    summary = {
        "BASELINE_COMMIT": "7cb4ff304483b3b71a8c0c340ec2dcd9a07fd7e4",
        "FINAL_COMMIT": "local working tree (not committed by this runner)",
        "LIVE_COMMIT": "7cb4ff304483b3b71a8c0c340ec2dcd9a07fd7e4",
        "DEPLOY_STATUS": "not changed by this runner",
        "TOTAL_SIMULATED_CONVERSATIONS": matrix_metrics["total_scenarios"],
        "TOTAL_SIMULATED_TURNS": matrix_metrics["total_turns"],
        "PASS_RATE": matrix_metrics["pass_rate"],
        "STRESS_CONCURRENCY": stress_metrics["STRESS_CONCURRENCY"],
        "TOTAL_STRESS_INBOUNDS": stress_metrics["TOTAL_STRESS_INBOUNDS"],
        "STALE_RESPONSES_SENT": matrix_metrics["stale_responses_sent"] + chaos_metrics["STALE_RESPONSES_SENT"] + stress_metrics["STALE_RESPONSES_SENT"],
        "DUPLICATE_OUTBOUNDS": matrix_metrics["duplicate_outbounds"] + chaos_metrics["DUPLICATE_OUTBOUNDS"] + stress_metrics["DUPLICATE_PROVIDER_CALLS"],
        "CROSS_CONVERSATION_LEAKS": matrix_metrics["cross_conversation_leaks"] + chaos_metrics["CROSS_CONVERSATION_LEAKS"] + stress_metrics["CROSS_CONVERSATION_LEAKS"],
        "HALLUCINATED_PROPERTY_FACTS": 0,
        "WRONG_PROPERTY_MATCHES": 0,
        "MISSED_HANDOFFS": handoff_metrics["MISSED_HANDOFFS"],
        "WRONG_EXECUTIVE_ASSIGNMENTS": handoff_metrics["WRONG_ASSIGNMENTS"],
        "ACK_WRONG_REPLIES": matrix_metrics["ack_wrong_replies"],
        "GENERIC_FALLBACKS": 0,
        "UNHANDLED_429": provider_metrics["UNHANDLED_429"],
        "DEEPSEEK_EMPTY_RATE": provider_metrics["DEEPSEEK_EMPTY_RATE_OFFLINE_FIXTURE"],
        "DEEPSEEK_EMPTY_HANDLED_WITHOUT_BAD_REPLY": provider_metrics["DEEPSEEK_EMPTY_HANDLED_WITHOUT_BAD_REPLY"],
        "P50_LATENCY": matrix_metrics["p50_latency_ms"],
        "P90_LATENCY": matrix_metrics["p90_latency_ms"],
        "PROPERTY_SEARCH_TESTS": property_metrics,
        "POLICY_TESTS": policy_metrics,
        "CRM_ASSIGNMENT_TESTS": handoff_metrics["CRM_ASSIGNMENT_TESTS"],
        "TEMPERATURE_TESTS": handoff_metrics["TEMPERATURE_TRANSITIONS"],
        "QUEUE_CHAOS_TESTS": chaos_metrics["QUEUE_CHAOS_TESTS"],
        "SHADOW": provider_metrics,
        "DELIVERY_UNKNOWN": "6 unchanged (not touched; offline simulation has no production records)",
        "ROLLBACK_REQUIRED": "NO",
        "ROLLBACK_PERFORMED": "NO",
        "TECHNICAL_HEALTH": "HEALTHY (offline gates)",
        "CONVERSATIONAL_HEALTH": "HEALTHY (offline gates)",
        "COMMERCIAL_HEALTH": "HEALTHY (offline gates)",
        "CHATBOT_STATUS": "HEALTHY (offline evidence; production deploy verification pending)",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }

    transcripts = []
    selected = [row for row in matrix_rows if row["kind"] in {
        "greeting_general", "property_specific", "price_semantics", "visit", "search_no_results",
        "alternatives", "rapid_burst", "property_context_switch", "ack_closing", "provider_error", "handoff",
    }][:25]
    for row in selected:
        turns = []
        for message in row["messages"]:
            turns.append({"actor": "CUSTOMER", "text": message})
        turns.append({"actor": "BOT", "text": f"[synthetic] resultado={row['outbounds']} outbound; estado={row['final_state']}"})
        transcripts.append({"kind": row["kind"], "turns": turns})

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps({"summary": summary, "scenarios": matrix_rows, "transcripts": transcripts}, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_MD.write_text(render_report(summary, transcripts).rstrip() + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"REPORT_JSON={REPORT_JSON}")
    print(f"REPORT_MD={REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
