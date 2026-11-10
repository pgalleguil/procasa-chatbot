"""Isolated multi-turn acceptance simulation for price-drop replies.

All database writes go to mongomock. The conversational generator is a
deterministic local stub, and the WaSender callback is a recording mock.
This script never reads production MongoDB credentials or calls a provider.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mongomock
from bson import ObjectId

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot import chatbot_queue as queue
from chatbot import price_drop_leads_v2 as v2
from chatbot import storage
from chatbot.conversation_policy import (
    is_visit_offer_question, outbound_unconfirmed_visit_claim,
)
from chatbot.price_drop_campaign_classifier import (
    ALTERNATIVES, DECLINED, INFO, INTEREST, NO_SIGNAL, PRIORITY,
    UNSUBSCRIBED, classify_price_drop_reply,
)
from chatbot.price_drop_pilot import context_instruction


BASE_TIME = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
TEMPERATURES = ("HOT", "NORMAL", "COLD")
PROPERTIES = (
    {"code": "6436", "commune": "San Miguel", "currency": "UF", "old": 2790, "new": 2690, "expenses": 98000},
    {"code": "6377", "commune": "Providencia", "currency": "UF", "old": 5123, "new": 5074, "expenses": 125000},
    {"code": "16479", "commune": "Lo Prado", "currency": "CLP", "old": 160000000, "new": 152000000, "expenses": 0},
    {"code": "90001", "commune": "Ñuñoa", "currency": "UF", "old": 3280, "new": 3190, "expenses": 112000},
    {"code": "90002", "commune": "La Reina", "currency": "CLP", "old": 198000000, "new": 191000000, "expenses": 148000},
    {"code": "90003", "commune": "Macul", "currency": "UF", "old": 2450, "new": 2390, "expenses": 76000},
)


def _format_price(amount: int, currency: str) -> str:
    if currency == "CLP":
        return "$" + f"{amount:,}".replace(",", ".")
    return f"UF {amount:,}".replace(",", ".")


def _campaign(event_number: int, prop: dict, old: int, new: int,
              sent_at: datetime) -> dict:
    campaign_id = f"sim_{prop['code']}_drop_{event_number}"
    old_field = "old_price_clp" if prop["currency"] == "CLP" else "old_price_uf"
    new_field = "new_price_clp" if prop["currency"] == "CLP" else "new_price_uf"
    message = (
        f"PROCASA informa rebaja del código {prop['code']}: "
        f"{_format_price(old, prop['currency'])} a {_format_price(new, prop['currency'])}."
    )
    return {
        "campaign_id": campaign_id,
        "event_id": campaign_id,
        "property_code": prop["code"],
        "phone_normalized": None,
        old_field: old,
        new_field: new,
        "currency": prop["currency"],
        "status": "SENT",
        "test_mode": False,
        "provider_message_id": f"mock-wa-out-{campaign_id}",
        "provider_status": "accepted",
        "outbound_message": message,
        "sent_at": sent_at,
        "expires_at": sent_at + timedelta(days=14),
        "reply_count": 0,
        "notifications": {
            "administrator": {"status": "NOT_TRIGGERED"},
            "property_executive": {"status": "FROZEN_ADMIN_ONLY"},
        },
    }


def _set_current_price(db, prop: dict, amount: int) -> None:
    pricing = {
        "precio_publicado": amount,
        "moneda_publicada": prop["currency"],
        "gastos_comunes": prop["expenses"],
    }
    if prop["currency"] == "UF":
        pricing["precio_uf"] = amount
    db["universo_cartera_prop360"].update_one(
        {"codigo": prop["code"]},
        {"$set": {
            "codigo": prop["code"], "disponible_prop360": True,
            "estado.oficina": "PROCASA SUCRE",
            "comuna": prop["commune"],
            "tipo_operacion.venta": True,
            "tipo_operacion.precio_venta": pricing,
        }}, upsert=True,
    )


def _add_history(db, prop: dict, old: int, new: int, when: datetime) -> None:
    db["universo_cartera_prop360_historial"].insert_one({
        "codigo": prop["code"], "campo": "precio_publicado",
        "fecha": when.isoformat(),
        "moneda_anterior": prop["currency"], "moneda_nueva": prop["currency"],
        "monto_anterior": old, "monto_nuevo": new,
    })


def _insert_successive_drop(db, lead_id, phone: str, prop: dict,
                            current_price: int, at: datetime) -> dict:
    second_price = current_price - (3000000 if prop["currency"] == "CLP" else 35)
    _set_current_price(db, prop, second_price)
    _add_history(db, prop, current_price, second_price, at - timedelta(seconds=30))
    campaign = _campaign(2, prop, current_price, second_price, at)
    campaign["phone_normalized"] = phone
    db["leads"].update_one(
        {"_id": lead_id},
        {"$push": {
            "price_drop_v2.campaigns": campaign,
            "messages": {
                "role": "assistant", "content": campaign["outbound_message"],
                "timestamp": at.isoformat(), "campaign_id": campaign["campaign_id"],
                "provider_message_id": campaign["provider_message_id"],
                "delivery_status": "accepted", "provider_status": "accepted",
            },
        }},
    )
    return campaign


def _case_templates(prop: dict) -> list[list[tuple[str, str]]]:
    amount = _format_price(prop["new"], prop["currency"])
    return [
        [
            ("Sí, me interesa la propiedad", INTEREST),
            ("No me llamen, pero quiero saber los gastos comunes", INFO),
            ("No quiero visitar todavía; ¿cuánto son los gastos comunes?", INFO),
            ("¿Tienen algo en Ñuñoa?", ALTERNATIVES),
        ],
        [
            ("¿Se puede visitar mañana?", PRIORITY),
            ("No puedo ir mañana; ¿cuánto son los gastos comunes?", INFO),
            (f"Te ofrezco {amount}", PRIORITY),
            ("¿Aceptan ofertas?", INFO),
        ],
        [
            ("Busco algo en Ñuñoa", ALTERNATIVES),
            ("¿Cuál es el precio actual?", INFO),
            (f"Podría pagar {amount}", PRIORITY),
            ("Me interesa, llámame el jueves", PRIORITY),
        ],
        [
            ("Sí me interesa, pero no quiero recibir llamadas", INTEREST),
            ("¿Incluye estacionamiento?", INFO),
            ("¿Aceptan ofertas?", INFO),
            ("No quiero recibir llamadas, pero sí mensajes por WhatsApp", INFO),
        ],
        [
            ("No me interesa visitar; ¿tiene bodega?", INFO),
            ("Quiero ver otras propiedades en Providencia", ALTERNATIVES),
            ("Quiero hacer una oferta por esta propiedad", PRIORITY),
            ("No quiero coordinar visita ahora", INFO),
        ],
        [
            ("Gracias por el aviso, ¿cuál es el precio actual?", INFO),
            ("Me parece interesante, ¿qué gastos comunes tiene?", INTEREST),
            ("Prefiero no agendar; ¿cuánto cuesta?", INFO),
            ("Encontré una vivienda y terminé mi búsqueda", UNSUBSCRIBED),
        ],
        [
            ("¿Todavía está disponible?", INFO),
            ("¿Y en La Reina?", ALTERNATIVES),
            ("¿Puedo hacer una oferta?", PRIORITY),
            ("No me llamen, por favor", INFO),
        ],
        [
            ("No me interesa esta propiedad", DECLINED),
            ("Sigo buscando, ¿cuál es el precio actual?", INFO),
            ("¿Tienen opciones en Ñuñoa?", ALTERNATIVES),
            ("No quiero recibir más información", UNSUBSCRIBED),
        ],
    ]


def _make_mock_response(*, text: str, classification, context: dict | None,
                        prop: dict) -> str:
    if classification.label == UNSUBSCRIBED or not context:
        return ""
    current = (
        context["new_price_clp"] if context["currency"] == "CLP"
        else context["new_price_uf"]
    )
    price = _format_price(int(current), context["currency"])
    code = context["property_code"]
    if classification.explicit_visit:
        return (
            f"Puedo revisar la disponibilidad para el código {code}. "
            "¿Qué horario te acomoda? La visita queda pendiente de confirmación."
        )
    if classification.explicit_offer:
        amount_match = re.search(r"(?:UF\s*)?\$?\s*[\d.]{4,}", text, re.IGNORECASE)
        amount = amount_match.group(0).strip() if amount_match else "tu propuesta"
        return (
            f"Gracias por compartir {amount} para el código {code}. "
            "¿Confirmas que ese es el monto que quieres proponer? No puedo confirmar su aceptación."
        )
    if classification.explicit_contact:
        return (
            f"Veo tu solicitud de llamada por el código {code}. "
            "¿Qué horario te suele acomodar? El contacto queda sujeto a confirmación del equipo."
        )
    if classification.alternatives_requested:
        return (
            f"Puedo revisar alternativas en la comuna que indicas. "
            f"¿Qué tipo de propiedad buscas además del código {code}?"
        )
    if "gastos comunes" in text.lower() or "gastos comunes" in text.casefold():
        return (
            f"Los gastos comunes informados para el código {code} son "
            f"${prop['expenses']:,} mensuales. El precio vigente es {price}."
        )
    if "precio" in text.lower() or "cuánto cuesta" in text.lower() or "cuánto son" in text.lower():
        return f"El código {code} está publicado actualmente en {price}."
    if classification.do_not_call:
        if "whatsapp" in text.lower() or "mensajes" in text.lower():
            return (
                f"Claro, seguimos por este medio. El código {code} figura a {price}. "
                "¿Qué otro detalle te gustaría revisar?"
            )
        return f"Entendido, seguimos por este medio sobre el código {code}. ¿Qué dato te gustaría revisar?"
    if classification.visit_declined:
        return f"Claro. Te comparto la información del código {code}; podemos seguir por aquí sin coordinar una visita."
    if classification.label == INTEREST:
        return f"Gracias por tu interés en el código {code}. El precio vigente es {price}. ¿Qué información te gustaría revisar?"
    if classification.label == DECLINED:
        return f"Entiendo. No insistiré con el código {code}. Si necesitas otro dato, puedes escribirnos por aquí."
    if classification.label == INFO:
        return f"El código {code} sigue disponible según el inventario simulado y figura a {price}."
    return f"Gracias por responder sobre el código {code}. Si surge una consulta concreta, puedo ayudarte por este medio."


def _naturalness_checks(text: str, response: str, classification, context: dict,
                        prop: dict) -> dict:
    if classification.label == UNSUBSCRIBED:
        return {"safe_optout_silence": response == ""}
    facts_ok = context["property_code"] in response
    current = (
        context["new_price_clp"] if context["currency"] == "CLP"
        else context["new_price_uf"]
    )
    if "precio" in text.lower() or "cuánto cuesta" in text.lower() or "cuánto son" in text.lower():
        facts_ok = facts_ok and _format_price(int(current), context["currency"]) in response
    if "gastos comunes" in text.lower():
        expense = f"${prop['expenses']:,}"
        facts_ok = facts_ok and expense in response
    if not classification.explicit_visit:
        facts_ok = facts_ok and not is_visit_offer_question(response)
        facts_ok = facts_ok and not outbound_unconfirmed_visit_claim(response)
    if classification.do_not_call:
        facts_ok = facts_ok and not re.search(r"\bllamad[ao]s?\b|\bllamar\b", response, re.I)
    natural = (
        0 < len(response) <= 600
        and response[:1].isupper()
        and response.endswith((".", "?", "!"))
        and "{" not in response and "}" not in response
    )
    return {"scripted_naturalness_rubric": natural, "verified_facts_and_guardrails": facts_ok}


def run_acceptance_matrix(output_path: Path | None = None) -> dict:
    """Run 40 four-turn conversations using actual queue/V2 persistence code."""
    from contextlib import ExitStack
    from unittest.mock import patch

    from chatbot import lead_router

    import os
    managed_env = (v2.CONTEXT_FLAG, "CHATBOT_BATCH_QUIET_SECONDS",
                   "CHATBOT_BATCH_MAX_WAIT_SECONDS")
    original_env = {name: os.environ.get(name) for name in managed_env}
    os.environ[v2.CONTEXT_FLAG] = "true"
    os.environ["CHATBOT_BATCH_QUIET_SECONDS"] = "1"
    os.environ["CHATBOT_BATCH_MAX_WAIT_SECONDS"] = "1"
    observations = []
    active_database = {"db": None}
    fake_sends = []
    db_get_calls = []

    def isolated_event(event_type, payload=None, *, db=None):
        target = db or active_database["db"]
        if target is not None:
            target["mock_observability_events"].insert_one({
                "event": event_type, "payload": payload or {},
                "timestamp": datetime.now(timezone.utc),
            })
        return f"mock-event-{len(observations) + 1}"

    def isolated_get_db(*args, **kwargs):
        # Some imported legacy modules request indexes at import time. Route
        # every such lookup to the active mongomock database, never MongoDB.
        target = active_database["db"]
        if target is None:
            target = mongomock.MongoClient()["isolated_module_init"]
        db_get_calls.append("mongomock")
        return target

    template_definitions = []
    for prop in PROPERTIES:
        for template in _case_templates(prop):
            template_definitions.append((prop, template))
    # Exactly forty complete transcripts: cycle six real-shaped inventory rows
    # across the eight independent response patterns.
    definitions = [template_definitions[i % len(template_definitions)] for i in range(40)]
    conversations = []

    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(storage, "record_observability_event", isolated_event))
            stack.enter_context(patch.object(storage, "get_db", isolated_get_db))
            import sys
            loaded_api = sys.modules.get("api_captacion")
            if loaded_api is not None and hasattr(loaded_api, "get_db"):
                stack.enter_context(patch.object(loaded_api, "get_db", isolated_get_db))
            stack.enter_context(patch.object(
                lead_router, "build_secure_crm_url",
                lambda lead: "https://crm.example.invalid/synthetic",
            ))
            for index, (prop, template) in enumerate(definitions, 1):
                db = mongomock.MongoClient()[f"acceptance_{index}"]
                active_database["db"] = db
                queue.ensure_queue_indexes(db)
                lead_id = ObjectId()
                phone = f"+5694400{index:04d}"
                prop_initial = dict(prop)
                _set_current_price(db, prop, prop["new"])
                drop_at = BASE_TIME - timedelta(days=2)
                _add_history(db, prop, prop["old"], prop["new"], drop_at)
                event1 = _campaign(1, prop, prop["old"], prop["new"], drop_at)
                event1["phone_normalized"] = phone
                first_inquiry_at = drop_at - timedelta(days=40)
                db["leads"].insert_one({
                    "_id": lead_id, "phone": phone,
                    "contact_phone_normalized": phone,
                    "phone_is_synthetic": True,
                    "lead_temperature_effective": TEMPERATURES[(index - 1) % 3],
                    "messages": [{
                        "role": "user",
                        "content": f"Consulté antes por la propiedad código {prop['code']}",
                        "timestamp": first_inquiry_at.isoformat(),
                    }, {
                        "role": "assistant", "content": event1["outbound_message"],
                        "timestamp": drop_at.isoformat(),
                        "campaign_id": event1["campaign_id"],
                        "provider_message_id": event1["provider_message_id"],
                        "delivery_status": "accepted", "provider_status": "accepted",
                    }],
                    "price_drop_v2": {"campaigns": [event1]},
                })

                use_successive_drop = (index % 2 == 0)
                turns = []
                prior_event_ids = []
                for turn_index, (text, expected) in enumerate(template, 1):
                    turn_at = BASE_TIME + timedelta(minutes=index * 10 + turn_index * 2)
                    if turn_index == 2 and use_successive_drop:
                        _insert_successive_drop(
                            db, lead_id, phone, prop, prop["new"],
                            turn_at - timedelta(minutes=1),
                        )
                    target_context = v2.active_context(
                        db, phone=phone, message=text, now=turn_at,
                    )
                    expected_campaign_id = (
                        f"sim_{prop['code']}_drop_2" if use_successive_drop and turn_index >= 2
                        else f"sim_{prop['code']}_drop_1"
                    )
                    context_ok = bool(
                        target_context and target_context["campaign_id"] == expected_campaign_id
                        and target_context["property_code"] == prop["code"]
                    )
                    classification = classify_price_drop_reply(text)
                    provider_inbound_id = f"mock-wa-in-{index}-{turn_index}"
                    recorded = v2.record_inbound(
                        db, phone=phone, provider_message_id=provider_inbound_id,
                        text=text, now=turn_at,
                    )
                    # The production webhook records V2 attribution after the
                    # durable queue write; the core turn persists the lead's
                    # user message afterwards. Keep that ordering here.
                    db["leads"].update_one({"_id": lead_id}, {"$push": {"messages": {
                        "role": "user", "content": text,
                        "timestamp": turn_at.isoformat(),
                        "provider_message_id": provider_inbound_id,
                    }}})
                    turn_state = {
                        "phone": phone, "text": text, "expected": expected,
                        "classification": classification,
                        "context": target_context,
                        "turn_at": turn_at,
                    }
                    async def mock_llm(_phone, _combined_text, *, batch_id=None,
                                       generation_id=None, **kwargs):
                        current = turn_state
                        cls = current["classification"]
                        response = _make_mock_response(
                            text=current["text"], classification=cls,
                            context=current["context"], prop=prop_initial,
                        )
                        if response:
                            db["leads"].update_one({"_id": lead_id}, {"$push": {"messages": {
                                "role": "assistant", "content": response,
                                "timestamp": current["turn_at"].isoformat(),
                                "batch_id": batch_id, "generation_id": generation_id,
                                "delivery_status": "generated",
                                "response_generator": "deterministic_mock",
                            }}})
                        return response

                    async def mock_wasender(_phone, message):
                        provider_id = f"mock-wa-response-{index}-{turn_index}"
                        fake_sends.append({"phone": _phone, "text": message,
                                           "provider_message_id": provider_id})
                        return {"success": True, "provider_message_id": provider_id,
                                "http_status": 200, "delivery_status": "accepted"}

                    job_id = queue.create_inbound_job(
                        db, inbound_provider_message_id=provider_inbound_id,
                        phone=phone, text=text,
                        received_at=turn_at - timedelta(seconds=2),
                    )
                    queue_result = asyncio.run(queue.process_one_batch(
                        db, worker_id=f"acceptance-{index}", llm=mock_llm,
                        sender=mock_wasender, now=turn_at,
                    ))
                    lead = db["leads"].find_one({"_id": lead_id}) or {}
                    v2_state = lead.get("price_drop_v2") or {}
                    campaigns = v2_state.get("campaigns") or []
                    campaign = next((x for x in campaigns
                                     if x.get("campaign_id") == expected_campaign_id), {})
                    campaign_message = next((x for x in (lead.get("messages") or [])
                                              if x.get("role") == "assistant"
                                              and x.get("campaign_id") == expected_campaign_id), {})
                    inbound_event = next((x for x in (v2_state.get("reply_events") or [])
                                          if x.get("provider_message_id") == provider_inbound_id), {})
                    assistant_rows = [x for x in (lead.get("messages") or [])
                                      if x.get("role") == "assistant"
                                      and x.get("batch_id") == (queue_result or {}).get("_id")]
                    response = assistant_rows[-1].get("content", "") if assistant_rows else ""
                    outcome = classify_price_drop_reply(text).label
                    naturalness = _naturalness_checks(
                        text, response, classification, target_context or {}, prop_initial,
                    ) if target_context else {"safe_optout_silence": response == ""}
                    if classification.label == UNSUBSCRIBED:
                        naturalness["global_suppression_persisted"] = (
                            v2_state.get("opted_out") is True
                            and lead.get("conversation_status") == "STOPPED_BY_CLIENT"
                            and response == ""
                        )
                    if classification.positive:
                        naturalness["admin_notice_queued_without_real_send"] = (
                            campaign.get("notifications", {}).get("administrator", {}).get("status")
                            in {"PENDING_HUMAN_REVIEW", "PENDING_SIMULATION"}
                        )
                    if classification.label == UNSUBSCRIBED:
                        naturalness["admin_notice_separated_from_client_send"] = (
                            campaign.get("notifications", {}).get("administrator", {}).get("status")
                            == "CANCELED"
                            and campaign.get("notifications", {}).get("administrator_info", {}).get("status")
                            == "PENDING_INFO"
                        )
                    if classification.label != UNSUBSCRIBED and response:
                        naturalness["provider_acceptance_is_not_delivery"] = (
                            assistant_rows[-1].get("delivery_status") == "accepted"
                            and assistant_rows[-1].get("provider_status") not in {"delivered", "read"}
                        )
                    checks = {
                        "classification_matches_expected": outcome == expected,
                        "attributed_to_correct_successive_event": context_ok and bool(inbound_event)
                            and inbound_event.get("campaign_id") == expected_campaign_id,
                        "reply_event_persisted": bool(inbound_event),
                        "inbound_message_persisted": any(
                            x.get("provider_message_id") == provider_inbound_id
                            and x.get("role") == "user" for x in lead.get("messages", [])
                        ),
                        "response_logged_in_leads_messages": (
                            classification.label == UNSUBSCRIBED or bool(assistant_rows)
                        ),
                        "approved_campaign_message_in_leads_messages": bool(campaign_message)
                            and campaign_message.get("content") == campaign.get("outbound_message")
                            and campaign_message.get("provider_status") == "accepted",
                        "queue_terminal_or_accepted": bool(queue_result)
                            and queue_result.get("state") == queue.ST_RESPONDED,
                        **naturalness,
                    }
                    visit_was_requested = classification.explicit_visit
                    if not visit_was_requested and response:
                        checks["no_unsolicited_visit_offer"] = (
                            not is_visit_offer_question(response)
                            and not outbound_unconfirmed_visit_claim(response)
                        )
                    checks["preference_flags_recorded"] = (
                        (not classification.do_not_call)
                        or (lead.get("price_drop_v2", {}).get("contact_preferences") or {})
                        .get("do_not_call") is True
                    )
                    if classification.visit_declined:
                        checks["visit_decline_is_not_property_rejection"] = (
                            classification.label != DECLINED
                            and lead.get("conversation_status") != "STOPPED_BY_CLIENT"
                        )
                    pass_turn = all(value is True for value in checks.values())
                    if expected_campaign_id not in prior_event_ids:
                        prior_event_ids.append(expected_campaign_id)
                    turns.append({
                        "turn": turn_index,
                        "input": text,
                        "response_generated": response,
                        "generator": "deterministic_mock_not_live_model",
                        "expected_classification": expected,
                        "obtained_classification": outcome,
                        "campaign_event_id": expected_campaign_id,
                        "inbound_record_result": recorded,
                        "queue_state": (queue_result or {}).get("state"),
                        "checks": checks,
                        "passed": pass_turn,
                        "evidence": {
                            "provider_inbound_id": provider_inbound_id,
                            "job_id": job_id,
                            "reply_event_campaign_id": inbound_event.get("campaign_id"),
                            "campaign_message_matches_event": bool(campaign_message)
                                and campaign_message.get("content") == campaign.get("outbound_message"),
                            "assistant_message_count_for_batch": len(assistant_rows),
                            "verified_property_code": (target_context or {}).get("property_code"),
                            "verified_currency": (target_context or {}).get("currency"),
                            "verified_current_price": (
                                (target_context or {}).get("new_price_clp")
                                if (target_context or {}).get("currency") == "CLP"
                                else (target_context or {}).get("new_price_uf")
                            ),
                        },
                    })

                    # Replaying the provider webhook must not duplicate events.
                    before = len(v2_state.get("reply_events") or [])
                    duplicate = v2.record_inbound(
                        db, phone=phone, provider_message_id=provider_inbound_id,
                        text=text, now=turn_at + timedelta(seconds=1),
                    )
                    after_lead = db["leads"].find_one({"_id": lead_id}) or {}
                    after = len((after_lead.get("price_drop_v2") or {}).get("reply_events") or [])
                    turns[-1]["checks"]["inbound_idempotency"] = (
                        duplicate.get("status") == "DUPLICATE_IGNORED" and before == after
                    )
                    turns[-1]["passed"] = all(
                        value is True for value in turns[-1]["checks"].values()
                    )

                conversations.append({
                    "conversation_id": f"synthetic-conversation-{index:02d}",
                    "lead_temperature": TEMPERATURES[(index - 1) % 3],
                    "property_code": prop["code"],
                    "currency": prop["currency"],
                    "successive_drop_simulated": use_successive_drop,
                    "campaign_events": prior_event_ids,
                    "turn_count": len(turns),
                    "turns": turns,
                    "passed": all(turn["passed"] for turn in turns),
                })
                observations.append({
                    "conversation_id": f"synthetic-conversation-{index:02d}",
                    "passed": conversations[-1]["passed"],
                    "turn_count": len(turns),
                })
    finally:
        for name, previous in original_env.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    flat_turns = [turn for conversation in conversations for turn in conversation["turns"]]
    matrix = {
        "title": "Aceptación aislada del clasificador de campañas de rebajas",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "reference_commit": "e46c1d7db9b3051d9ae0e833e482b7d5cb726337",
        "execution": {
            "conversation_count": len(conversations),
            "turn_count": len(flat_turns),
            "mongo": "mongomock only; get_db patched to return the isolated in-memory database",
            "get_db_calls_routed_to_mongomock": len(db_get_calls),
            "wasender": "fully replaced with recording mock; provider was not contacted",
            "generator": "deterministic Spanish callback injected into the real durable queue",
            "live_generator_used": False,
            "live_generator_configured_in_environment": bool(
                os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
            ),
            "production_effects": False,
            "inventory": "synthetic Prop360 rows with UF and CLP histories",
            "temperatures": {value: sum(1 for c in conversations if c["lead_temperature"] == value)
                             for value in TEMPERATURES},
            "successive_drop_conversations": sum(1 for c in conversations
                                                  if c["successive_drop_simulated"]),
            "mock_customer_provider_acceptances": len(fake_sends),
            "customer_optout_responses_sent": sum(
                1 for turn in flat_turns
                if turn["expected_classification"] == UNSUBSCRIBED
                and turn["response_generated"]
            ),
        },
        "summary": {
            "passed_conversations": sum(1 for c in conversations if c["passed"]),
            "failed_conversations": sum(1 for c in conversations if not c["passed"]),
            "passed_turns": sum(1 for turn in flat_turns if turn["passed"]),
            "failed_turns": sum(1 for turn in flat_turns if not turn["passed"]),
            "critical_issue_open": any(not c["passed"] for c in conversations),
            "acceptance_status": "PASS" if all(c["passed"] for c in conversations) else "FAIL",
            "limitations": [
                "The production LLM generator was not configured or invoked; conversational generation in this matrix is a deterministic test double.",
                "Naturalness is checked against a scripted Spanish-language quality rubric, not a live model or human panel.",
                "Provider acceptance receipts in this simulation are mocks and do not represent delivery or WhatsApp policy approval.",
            ],
        },
        "matrix": conversations,
    }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8")
    return matrix


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=PROJECT_ROOT / "reports" / "price_drop_classifier_acceptance_2026-11-09.json",
        help="JSON report path (default: reports/price_drop_classifier_acceptance_2026-11-09.json)",
    )
    args = parser.parse_args()
    result = run_acceptance_matrix(args.output)
    print(json.dumps({
        "report": str(args.output),
        "summary": result["summary"],
        "execution": result["execution"],
    }, ensure_ascii=False, indent=2))
    return 0 if result["summary"]["acceptance_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
