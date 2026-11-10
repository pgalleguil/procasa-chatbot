"""Run an isolated, real-DeepSeek price-drop conversation acceptance matrix.

This is an explicitly invoked acceptance run, not a CI test.  It sends only
synthetic property/customer text to the configured DeepSeek endpoint. MongoDB
is replaced with mongomock, WaSender and administrative delivery are injected
mock senders, and the repository .env is never loaded wholesale.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import io
import json
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_ENV = Path.home() / "Desktop" / "Python" / "ChatBot_v4_Grok" / ".env"
COMMUNES = [
    "Ñuñoa", "La Reina", "Macul", "Providencia", "San Miguel",
    "Las Condes", "Peñalolén", "Santiago", "La Florida", "Vitacura",
]


def _load_provider_only(env_file: Path) -> tuple[str, str]:
    """Read only DeepSeek credential/endpoint values, never Mongo/WaSender."""
    from dotenv import dotenv_values

    values = dotenv_values(env_file)
    key = str(values.get("DEEPSEEK_API_KEY") or values.get("XAI_API_KEY") or "").strip()
    base_url = str(values.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip()
    parsed = urlparse(base_url)
    if not key:
        raise RuntimeError("deepseek_api_key_missing_in_private_env")
    if parsed.scheme != "https" or parsed.hostname != "api.deepseek.com":
        raise RuntimeError("deepseek_endpoint_not_official_api_host")
    return key, base_url.rstrip("/")


def _scenario(profile: int, variant: int, *, code: str,
              commune: str, other_commune: str, currency: str,
              old_price: int, new_price: int) -> dict:
    uf_old = f"UF {old_price:,}".replace(",", ".")
    uf_new = f"UF {new_price:,}".replace(",", ".")
    clp_old = "$" + f"{old_price:,}".replace(",", ".")
    clp_new = "$" + f"{new_price:,}".replace(",", ".")
    old_text, new_text = (uf_old, uf_new) if currency == "UF" else (clp_old, clp_new)

    profiles = [
        {
            "name": "precio_y_ficha",
            "turns": [
                ([f"¿Cómo queda el precio del código {code} después de la rebaja?",
                  f"¿Cuál es el valor vigente de la propiedad {code} ahora?",
                  f"Vi que rebajó la propiedad {code}; ¿qué precio tiene actualmente?",
                  f"¿Cuánto bajó la vivienda {code} y cuál es su precio actual?"][variant], "CONSULTA_INFORMATIVA"),
                (["¿Qué gastos comunes aparecen en la ficha?",
                  "¿Cuántos dormitorios y baños informa Prop360?",
                  "¿Qué comuna y superficie útil están registradas?",
                  "¿Qué orientación tiene y sigue activa según la ficha?"][variant], "CONSULTA_INFORMATIVA"),
            ],
        },
        {
            "name": "interes_sin_llamadas",
            "turns": [
                ([f"Sí me interesa la propiedad {code}, pero no quiero que me llamen. ¿Qué detalle puedes contarme?",
                  f"Me interesa el código {code}; prefiero que no me contacten por teléfono.",
                  "Sigo interesada, aunque no acepto llamadas; podemos continuar por aquí.",
                  "Sí, me interesa, pero por favor no me llamen para nada."][variant], "INTERES_DECLARADO"),
                (["Mejor sigamos por mensajes. ¿Cuánto bajó el valor vigente?",
                  "¿Qué gastos comunes están informados en la publicación?",
                  "¿La ficha señala cuántos estacionamientos incluye?",
                  "¿Cuál es la comuna y el metraje útil publicado?"][variant], "CONSULTA_INFORMATIVA"),
            ],
        },
        {
            "name": "rechazo_solo_visita",
            "turns": [
                (["No me interesa visitar por ahora, pero dime cuánto son los gastos comunes.",
                  "No quisiera coordinar una visita todavía; ¿cuántos dormitorios tiene?",
                  "No quiero ir a verla por el momento, aunque sí necesito saber la orientación.",
                  "No estoy interesada en visitar, pero quisiera conocer el precio publicado."][variant], "CONSULTA_INFORMATIVA"),
                (["¿Y cuántos dormitorios tiene el departamento?",
                  "¿La propiedad sigue activa según Prop360?",
                  "¿La ficha informa estacionamiento y bodega?",
                  "¿Qué gastos comunes registra la publicación?"][variant], "CONSULTA_INFORMATIVA"),
            ],
        },
        {
            "name": "solicitud_visita",
            "turns": [
                ([f"Quiero coordinar una visita a la propiedad {code}, idealmente el sábado. ¿Qué sigue?",
                  f"¿Puedo visitar la propiedad {code} este fin de semana?",
                  "Me gustaría ir a conocer el departamento; ¿cómo coordinamos una visita?",
                  "Quisiera visitar esta propiedad el viernes, si existe disponibilidad."][variant], "PRIORIDAD_COMERCIAL"),
                (["¿Puedo visitar el sábado por la mañana?",
                  "¿Qué día podría ir a verla?",
                  "Me acomoda conocerla el domingo, ¿pueden revisar disponibilidad?",
                  "Sí, quisiera coordinar la visita para el viernes."][variant], "PRIORIDAD_COMERCIAL"),
            ],
        },
        {
            "name": "solicitud_contacto",
            "turns": [
                ([f"Me gustaría que me llame un ejecutivo para conversar sobre la propiedad {code}.",
                  "¿Puede contactarme un asesor para hablar de esta propiedad?",
                  "Quisiera hablar con una ejecutiva sobre las condiciones de venta.",
                  "Por favor, que me contacte un asesor para resolver unas dudas."][variant], "PRIORIDAD_COMERCIAL"),
                (["¿Podrían escribirme primero por este chat?",
                  "También necesito conversar con una persona del equipo.",
                  "¿Me pueden contactar para revisar los antecedentes?",
                  "Prefiero recibir la información por escrito antes de hablar."][variant],
                 "CONSULTA_INFORMATIVA" if variant == 3 else "PRIORIDAD_COMERCIAL"),
            ],
        },
        {
            "name": "oferta",
            "turns": [
                ([f"Quisiera presentar una oferta de {new_text} por la propiedad {code}; ¿cómo la canalizo?",
                  f"Quiero ofrecer {new_text} por el departamento. ¿Cuál es el siguiente paso?",
                  "Me interesa hacer una oferta por esta vivienda, ¿con quién la reviso?",
                  f"¿Puedo ofertar {new_text} por el código {code}?"][variant], "PRIORIDAD_COMERCIAL"),
                (["¿La oferta debe incluir condiciones de pago?",
                  "¿El propietario revisa una propuesta formal?",
                  "¿Qué antecedentes necesito revisar sobre el financiamiento?",
                  "¿Cómo se presentan los antecedentes de la oferta?"][variant], "CONSULTA_INFORMATIVA"),
            ],
        },
        {
            "name": "alternativas_otra_comuna",
            "turns": [
                ([f"¿Me puedes mostrar otras propiedades en {commune} con un precio parecido?",
                  f"¿Podrías buscar opciones en {commune}?",
                  f"¿Pueden buscar propiedades en otra comuna, por ejemplo {commune}?",
                  f"Busco un departamento en {commune}; ¿tienen algo similar?"][variant], "BUSCA_ALTERNATIVAS"),
                ([f"¿Podrías buscar opciones en {other_commune}?",
                  f"También me sirven otras alternativas por {other_commune}.",
                  f"¿Hay otra propiedad disponible en {other_commune}?",
                  f"Estoy buscando algo por {other_commune}."][variant], "BUSCA_ALTERNATIVAS"),
            ],
        },
        {
            "name": "rechazo_propiedad_y_busca_alternativas",
            "turns": [
                ([f"No me interesa esta propiedad; ¿pueden buscar opciones similares en {commune}?",
                  f"Descarto el código {code}, pero quisiera ver otras propiedades en {commune}.",
                  f"Esta vivienda no me acomoda; ¿me muestran alternativas por {commune}?",
                  f"No quiero esta propiedad. ¿Podrían recomendarme otra en {commune}?"][variant], "BUSCA_ALTERNATIVAS"),
                ([f"Sí, busco algo por {other_commune}.",
                  f"¿Tienen otras propiedades en {other_commune}?",
                  f"Me gustaría ver otra comuna, como {other_commune}.",
                  f"¿Pueden enviarme alternativas en {other_commune}?"][variant], "BUSCA_ALTERNATIVAS"),
            ],
        },
        {
            "name": "baja_global",
            "turns": [
                ([f"¿Cómo queda el valor de la propiedad {code} después de la rebaja?",
                  "¿Qué precio vigente tiene la propiedad que consulté?",
                  "¿Cuánto bajó el valor publicado?",
                  "¿Qué datos principales tiene la vivienda rebajada?"][variant], "CONSULTA_INFORMATIVA"),
                (["No quiero recibir más información, por favor.",
                  "No quiero que me sigan escribiendo ni enviando ofertas.",
                  "Por favor, dejen de contactarme; no deseo más mensajes.",
                  "No kiero resivir mas informacion, porfa."][variant], "BAJA"),
            ],
        },
        {
            "name": "busqueda_terminada",
            "turns": [
                (["¿Qué ventajas tiene la rebaja actual de la vivienda?",
                  f"¿Cuál es el valor actual del código {code}?",
                  "¿Qué aspectos de la ficha debería revisar antes de decidir?",
                  "¿Qué precio aparece publicado para la propiedad?"][variant], "CONSULTA_INFORMATIVA"),
                (["Encontré una vivienda en otra parte y terminé mi búsqueda, gracias.",
                  "Ya conseguí una casa y mi búsqueda quedó resuelta.",
                  "Encontré un departamento y ya no necesito seguir buscando.",
                  "Por suerte encontré una propiedad, ya cerré la búsqueda."][variant], "BAJA"),
            ],
        },
    ]
    result = dict(profiles[profile])
    result["currency"] = currency
    result["old_price_display"] = old_text
    result["new_price_display"] = new_text
    result["code"] = code
    result["commune"] = commune
    return result


def _property_document(code: str, *, currency: str, current_price: int,
                       commune: str, variation: int) -> dict:
    price = {
        "precio_publicado": current_price,
        "moneda_publicada": currency,
    }
    if currency == "UF":
        price["precio_uf"] = current_price
        price["precio_clp"] = current_price * 39_000
    else:
        price["precio_clp"] = current_price
    return {
        "codigo": code,
        "disponible_prop360": True,
        "estado": {"oficina": "PROCASA SUCRE", "ejecutivo": "Equipo sintético"},
        "tipo_operacion": {
            "venta": True,
            "tipo": "Departamento" if variation % 2 == 0 else "Casa",
            "precio_venta": price,
            "gastos_comunes": 70_000 + 5_000 * (variation % 8),
        },
        "ubicacion": {"region": "Metropolitana", "comuna": commune},
        "caracteristicas": {
            "dormitorios": 1 + variation % 4,
            "banos": 1 + variation % 3,
            "superficie_util": 45 + variation % 75,
            "superficie_total": 50 + variation % 80,
            "estacionamientos": variation % 2,
            "orientacion": ["Norte", "Sur", "Oriente", "Poniente"][variation % 4],
        },
    }


def _campaign_row(*, campaign_id: str, code: str, currency: str,
                  old_price: int, new_price: int, sent_at: dt.datetime,
                  expires_at: dt.datetime, event_id: str) -> dict:
    row = {
        "campaign_id": campaign_id,
        "event_id": event_id,
        "history_event_ids": [event_id],
        "property_code": code,
        "currency": currency,
        "status": "SENT",
        "test_mode": False,
        "sent_at": sent_at,
        "expires_at": expires_at,
        "provider_message_id": f"synthetic-outbound-{campaign_id}",
        "provider_status": "accepted",
        "outbound_message": f"Aviso sintético de rebaja para {code}.",
        "reply_count": 0,
        "notifications": {
            "administrator": {"status": "NOT_TRIGGERED"},
            "property_executive": {"status": "FROZEN_ADMIN_ONLY"},
            "administrator_info": {"status": "NOT_TRIGGERED"},
        },
    }
    if currency == "UF":
        row.update(old_price_uf=old_price, new_price_uf=new_price)
    else:
        row.update(old_price_clp=old_price, new_price_clp=new_price)
    return row


def _contains_visit_invitation(text: str) -> bool:
    value = str(text or "").casefold()
    patterns = (
        r"\b(?:te\s+invito|cuando\s+quieras|si\s+quieres|"
        r"te\s+gustar[ií]a|podr[ií]as|puedes|quieres|"
        r"me\s+gustar[ií]a)\b.{0,45}\b(?:visitar|visita|conocer)\b",
        r"\b(?:coordinemos|coordinamos|agenda|agendemos|"
        r"programemos)\b.{0,35}\bvisita\b",
    )
    if not any(re.search(pattern, value) for pattern in patterns):
        return False
    return not bool(re.search(
        r"\b(?:no\s+(?:te\s+)?(?:invito|propongo|ofrezco)|"
        r"no\s+(?:quiero|quieres|quieras|deseo|deseas|desees|"
        r"necesito|necesitas)\b.{0,40}\b"
        r"(?:coordinar|agendar|programar|visita|visitar|conocer)|"
        r"sin\s+proponer)\b", value,
    ))


def _requested_communes(message: str) -> list[str]:
    import unicodedata
    normalized = unicodedata.normalize("NFKD", str(message or "").casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    result = []
    for commune in COMMUNES:
        folded = unicodedata.normalize("NFKD", commune.casefold())
        folded = "".join(char for char in folded if not unicodedata.combining(char))
        if folded and re.search(rf"(?<![a-z]){re.escape(folded)}(?![a-z])", normalized):
            result.append(commune)
    return result


def _campaign_final_response_checks(message: str, response: str,
                                    classification: dict, *,
                                    original_commune: str | None = None,
                                    property_doc: dict | None = None) -> dict:
    from chatbot.customer_response_semantics import evaluate_price_drop_final_response
    return evaluate_price_drop_final_response(
        message, response, classification,
        original_commune=original_commune,
        requested_communes=_requested_communes(message),
        property_doc=property_doc,
    )


def _reevaluate_saved_matrix(report: dict) -> dict:
    """Reapply the current deterministic acceptance checks to stored turns."""
    previous_turn_issues = {
        issue for conversation in report.get("conversation_matrix") or []
        for turn in conversation.get("turns") or []
        for issue in turn.get("issues") or []
    }
    for conversation in report.get("conversation_matrix") or []:
        for turn in conversation.get("turns") or []:
            issues = [
                issue for issue in turn.get("issues") or []
                if issue != "visit_proposed_without_customer_request"
                and not issue.startswith("final_response_semantics:")
            ]
            classification = turn.get("classification_details") or {}
            semantic_checks = _campaign_final_response_checks(
                turn.get("input") or "",
                turn.get("customer_visible_reply") or "",
                classification,
                original_commune=conversation.get("commune"),
            )
            turn["final_response_semantics"] = semantic_checks
            for check, passed in semantic_checks.items():
                if check != "PASS" and not passed:
                    issues.append(f"final_response_semantics:{check}")
            requested = bool(classification.get("visit_requested"))
            unsolicited = not requested and _contains_visit_invitation(
                turn.get("customer_visible_reply") or ""
            )
            turn["visit_offer_without_request"] = unsolicited
            if unsolicited:
                issues.append("visit_proposed_without_customer_request")
            turn["issues"] = sorted(set(issues))
    remaining_turn_issues = {
        issue for conversation in report.get("conversation_matrix") or []
        for turn in conversation.get("turns") or []
        for issue in turn.get("issues") or []
    }
    global_only = set(report.get("issues") or []) - previous_turn_issues
    report["issues"] = sorted(global_only | remaining_turn_issues)
    summary = report.setdefault("summary", {})
    all_turns = [
        turn for conversation in report.get("conversation_matrix") or []
        for turn in conversation.get("turns") or []
    ]
    summary["visit_offer_guard_passes"] = sum(
        not turn.get("visit_offer_without_request") for turn in all_turns
    )
    summary["final_response_relevance_passes"] = sum(
        (turn.get("final_response_semantics") or {}).get("PASS") is True
        for turn in all_turns
    )
    summary["alternative_responses_passes"] = sum(
        all((turn.get("final_response_semantics") or {}).get(key) is True for key in (
            "ALTERNATIVE_REQUEST_ANSWERED",
            "ALTERNATIVE_TARGETS_ADDRESSED",
            "NO_ORIGINAL_COMMUNE_SUBSTITUTION",
        ))
        for turn in all_turns
    )
    summary["offer_routing_passes"] = sum(
        (turn.get("final_response_semantics") or {}).get("OFFER_HANDLER_ANSWERED") is True
        for turn in all_turns
    )
    report["evaluation_replay"] = {
        "evaluator_version": "price_drop_acceptance_rules_v3",
        "replayed_turns": len(all_turns),
        "regenerated_model_replies": 0,
        "note": (
            "Rechecked the saved real-provider conversation matrix; no model, "
            "WhatsApp, database, or administrative delivery calls were made."
        ),
    }
    if report["issues"]:
        report["status"] = "FAIL"
    elif report.get("generation_mode") == "simulated_mock":
        report["status"] = "SIMULATED"
    elif report.get("targeted_run") and report.get("conversation_count", 0) > 0:
        report["status"] = "TARGETED_PASS"
    elif report.get("conversation_count", 0) >= 40:
        report["status"] = "PASS"
    else:
        report["status"] = "FAIL"
    return report


def _price_token_present(text: str, *, currency: str, amount: int) -> bool:
    if currency == "UF":
        expected = f"{amount:,}".replace(",", ".")
        # Permit normal spacing and optional decimal zeroes, but not a rounded value.
        return bool(re.search(rf"\bUF\s*{re.escape(expected)}(?:,00|\.00)?\b", text, re.I))
    digits = f"{amount:,}".replace(",", ".")
    return bool(re.search(rf"\$\s*{re.escape(digits)}\b", text))


def _verify_listing_price_claims(text: str, *, currency: str,
                                old_price: int, new_price: int) -> list[dict]:
    """Validate explicit listing-price claims; ignore fees and financing figures."""
    allowed = {currency: {old_price, new_price, abs(old_price - new_price)}}
    if currency == "UF":
        allowed["CLP"] = {
            int(value * 39_000)
            for value in (old_price, new_price, abs(old_price - new_price))
        }
    price_labels = re.compile(
        r"\b(?:precio|valor|rebaj\w*|baj\w*|pas[oó]|antes|actual|vigente|"
        r"publicad\w*|referencia|equivale\w*|menos)\b", re.I,
    )
    non_price_labels = re.compile(
        r"\b(?:gastos\s+comunes|gc|contribuciones|dividendo|pie|oferta|"
        r"ofrezc\w*|ofert\w*|propuesta|notar[ií]a|cr[eé]dito)\b", re.I,
    )
    claims = []
    for match in re.finditer(r"\bUF\s*([\d.]+)|\$\s*([\d.]+)", str(text or ""), re.I):
        raw = match.group(1) or match.group(2) or ""
        digits = re.sub(r"\D", "", raw)
        amount = int(digits) if digits else None
        prefix = str(text or "")[max(0, match.start() - 70):match.start()]
        suffix = str(text or "")[match.end():min(len(str(text or "")), match.end() + 25)]
        preceding_labels = list(price_labels.finditer(prefix)) + list(non_price_labels.finditer(prefix))
        nearest_label = max(preceding_labels, key=lambda item: item.end()).group(0).casefold() if preceding_labels else ""
        after_labels = list(price_labels.finditer(suffix)) + list(non_price_labels.finditer(suffix))
        following_label = min(after_labels, key=lambda item: item.start()).group(0).casefold() if after_labels else ""
        if nearest_label and non_price_labels.fullmatch(nearest_label):
            kind = "non_listing_amount"
            verified = True
        elif following_label and non_price_labels.fullmatch(following_label) and not (
            nearest_label and price_labels.fullmatch(nearest_label)
        ):
            kind = "non_listing_amount"
            verified = True
        elif (nearest_label and price_labels.fullmatch(nearest_label)) or (
            following_label and price_labels.fullmatch(following_label)
        ):
            amount_currency = "UF" if match.group(1) else "CLP"
            kind = "listing_price"
            verified = amount is not None and amount in allowed.get(amount_currency, set())
        else:
            kind = "unclassified_amount"
            verified = True
        claims.append({
            "currency": "UF" if match.group(1) else "CLP",
            "amount": amount,
            "kind": kind,
            "verified": verified,
        })
    return claims


def _property_numeric_fact_mismatches(reply: str, *, variation: int) -> list[str]:
    """Check numeric listing facts against the deterministic synthetic record."""
    expected = {
        "bedrooms": 1 + variation % 4,
        "bathrooms": 1 + variation % 3,
        "useful_surface": 45 + variation % 75,
        "total_surface": 50 + variation % 80,
        "parking": variation % 2,
        "orientation": ["norte", "sur", "oriente", "poniente"][variation % 4],
    }
    text = str(reply or "").casefold()
    mismatches = []
    patterns = {
        "bedrooms": (
            r"\b(\d{1,2})[ \t]+dormitorio(?:s)?\b",
            r"\bdormitorios?\s*[:=]\s*(\d{1,2})\b",
        ),
        "bathrooms": (
            r"\b(\d{1,2})[ \t]+baño(?:s)?\b",
            r"\bbaños?\s*[:=]\s*(\d{1,2})\b",
        ),
        "useful_surface": (
            r"\b(\d{1,3})[ \t]*m(?:²|2)[ \t]*(?:útil|util)(?:es)?\b",
            r"\bsuperficie[ \t]+(?:útil|util)\s*[:=]\s*(\d{1,3})\b",
        ),
        "total_surface": (
            r"\b(\d{1,3})[ \t]*m(?:²|2)[ \t]*total(?:es)?\b",
            r"\bsuperficie[ \t]+total\s*[:=]\s*(\d{1,3})\b",
        ),
    }
    for fact, fact_patterns in patterns.items():
        for pattern in fact_patterns:
            for match in re.finditer(pattern, text):
                if int(match.group(1)) != expected[fact]:
                    mismatches.append(f"{fact}_claim_mismatch")
    for match in re.finditer(r"\borientaci[oó]n\s+(?:es\s+)?(norte|sur|oriente|poniente)\b", text):
        if match.group(1) != expected["orientation"]:
            mismatches.append("orientation_claim_mismatch")
    negative_parking = bool(re.search(
        r"\b(?:sin|no\s+(?:incluye|considera|tiene|cuenta\s+con))\s+"
        r"(?:estacionamiento|estacionamientos)\b", text,
    ))
    positive_parking = bool(re.search(
        r"\b(?:con|incluye|considera|tiene)\s+"
        r"(?:\d\s+)?estacionamiento(?:s)?\b", text,
    ))
    if (negative_parking and expected["parking"] > 0) or (
        positive_parking and expected["parking"] == 0
    ):
        mismatches.append("parking_claim_mismatch")
    return sorted(set(mismatches))


async def _synthetic_test_run(args) -> dict:
    mock_deepseek = bool(args.mock_deepseek)
    if mock_deepseek:
        key, base_url = "acceptance-mock-no-network", "https://api.deepseek.com"
    else:
        key, base_url = _load_provider_only(args.env_file)
    # Do not let application config read any local .env or inherit production
    # database/provider secrets in this isolated process.
    for name in (
        "MONGO_URI", "WASENDER_TOKEN", "WASENDER_WEBHOOK_SECRET",
        "WASENDER_API_KEY", "WASENDER_SESSION_ID", "WASENDER_BASE_URL",
        "PRICE_DROP_LEADS_V2_SEND_ENABLED", "PRICE_DROP_LEADS_V2_REAL_PILOT_ENABLED",
        "PRICE_DROP_LEADS_V2_REAL_CAMPAIGNS_ENABLED",
        "PRICE_DROP_LEADS_V2_EXECUTIVE_NOTICE_ENABLED",
        "PRICE_DROP_LEADS_V2_REAL_ADMIN_NOTICES_ENABLED",
    ):
        os.environ.pop(name, None)
    os.environ.update({
        "APP_ENV": "test",
        "DEEPSEEK_API_KEY": key,
        "DEEPSEEK_BASE_URL": base_url,
        "PRICE_DROP_LEADS_V2_CONTEXT_ENABLED": "true",
        "CHATBOT_BATCH_QUIET_SECONDS": "1",
        "CHATBOT_BATCH_MAX_WAIT_SECONDS": "1",
    })
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False

    import mongomock
    from bson import ObjectId
    import config
    if config.Config.MONGO_URI:
        raise RuntimeError("isolated_run_refuses_mongo_uri")
    if config.Config.WASENDER_TOKEN:
        raise RuntimeError("isolated_run_refuses_wasender_credentials")
    if config.Config.IS_PRODUCTION:
        raise RuntimeError("isolated_run_refuses_production_config")

    from chatbot import storage
    fake_client = mongomock.MongoClient(tz_aware=True)
    db = fake_client["URLS"]
    storage.get_db = lambda: db
    from chatbot import chatbot_queue as queue
    from chatbot import core, grok_client
    core.get_db = lambda: db
    core.es_propietario = lambda _phone: (False, None)
    core.buscar_propiedades = lambda *a, **kw: []
    core.buscar_semanticamente = lambda *a, **kw: []

    external_alert_calls: list[dict] = []
    async def mocked_alert(*a, **kw):
        external_alert_calls.append({"type": "core_alert", "called": True})
        return {"status": "mocked"}
    core.send_alert_once = mocked_alert

    provider_calls = 0
    completions = grok_client.client.chat.completions
    original_create = completions.create
    def counted_create(*a, **kw):
        nonlocal provider_calls
        if mock_deepseek:
            raise RuntimeError("mock_deepseek_network_is_disabled")
        if provider_calls >= int(os.environ.get("PRICE_DROP_ACCEPTANCE_CALLS_LIMIT", "220")):
            raise RuntimeError("deepseek_acceptance_call_limit_reached")
        provider_calls += 1
        return original_create(*a, **kw)
    completions.create = counted_create

    traces: dict[str, list[dict]] = {}
    original_generator = grok_client.generar_respuesta_estructurada
    def observed_generator(messages, prospecto_actual=None, telemetry_context=None, **kwargs):
        api_calls_before = provider_calls
        diagnostic = {}
        # Phone numbers are not needed for generation/extraction. Strip them
        # before the synthetic prompt leaves this process.
        safe_prospect = dict(prospecto_actual or {})
        safe_prospect.pop("phone", None)
        if mock_deepseek:
            context_text = "\n".join(
                str(row.get("content") or "") for row in messages
                if row.get("role") == "system"
            )
            user_texts = [
                str(row.get("content") or "") for row in messages
                if row.get("role") == "user"
            ]
            current_user_text = user_texts[-1] if user_texts else ""
            from chatbot.price_drop_campaign_classifier import classify_price_drop_reply
            classification = classify_price_drop_reply(current_user_text)
            intent_by_campaign_signal = {
                "ASK_VISIT": "agendar_visita",
                "ASK_CONTACT": "contacto_directo",
            }
            current_price = re.search(
                r"rebajada de (UF [0-9.]+|\$[0-9.]+) a (UF [0-9.]+|\$[0-9.]+)",
                context_text,
            )
            price = current_price.group(2) if current_price else "el valor verificado"
            code = str(safe_prospect.get("codigo") or "la propiedad")
            diagnostic.update({"provider": "mock", "model": "synthetic-response-stub",
                               "provider_calls": 0})
            result = {
                "intencion": intent_by_campaign_signal.get(
                    classification.intent, "consulta_general"
                ), "datos_extraidos": {},
                "respuesta_bot": (
                    f"El precio vigente de la propiedad {code} es {price}. "
                    "La ficha conserva los datos verificados y puedo aclarar cualquier detalle."
                ),
            }
        else:
            result = original_generator(
                messages, safe_prospect, telemetry_context,
                diagnostics=diagnostic, **kwargs,
            )
        batch_id = str((telemetry_context or {}).get("batch_id") or "unknown")
        user_texts = [str(row.get("content") or "") for row in messages if row.get("role") == "user"]
        current_text = user_texts[-1] if user_texts else ""
        deduplicated_user_texts = list(user_texts)
        if len(deduplicated_user_texts) >= 2 and deduplicated_user_texts[-2] == current_text:
            deduplicated_user_texts.pop()
        prior_user_count = max(len(deduplicated_user_texts) - 1, 0)
        turn_trace = {
            "provider": diagnostic.get("provider", "deepseek"),
            "model": diagnostic.get("model") or config.Config.DEEPSEEK_MODEL_REASONER,
            "provider_calls_reported": diagnostic.get("provider_calls", 0),
            "provider_api_calls_observed": provider_calls - api_calls_before,
            "format_retries": diagnostic.get("format_retries", 0),
            "provider_error": diagnostic.get("error"),
            "structured_generation_status": (
                "MODEL_FALLBACK_AFTER_STRUCTURED_ERROR"
                if diagnostic.get("error") and result.get("respuesta_bot")
                else "STRUCTURED_JSON"
                if result.get("respuesta_bot")
                else "NO_MODEL_REPLY"
            ),
            "response_candidate": str(result.get("respuesta_bot") or ""),
            "pilot_context_in_prompt": any(
                "[PILOTO DE REACTIVACIÓN POR REBAJA" in str(row.get("content") or "")
                for row in messages if row.get("role") == "system"
            ),
            "previous_user_turns_in_prompt": prior_user_count,
        }
        traces.setdefault(batch_id, []).append(turn_trace)
        return result
    core.generar_respuesta_estructurada = observed_generator

    queue.ensure_queue_indexes(db)
    now = dt.datetime.now(dt.timezone.utc)
    fixtures = []
    temperature_names = ["HOT", "NORMAL", "COLD"]
    for index in args.scenario_indices:
        profile, variant = index % 10, (index // 10) % 4
        code = str(12000 + index)
        commune = COMMUNES[(index * 3) % len(COMMUNES)]
        other_commune = COMMUNES[(index * 3 + 4) % len(COMMUNES)]
        currency = "UF" if index % 2 == 0 else "CLP"
        current_price = (2_650 + (index % 13) * 85 if currency == "UF"
                         else 125_000_000 + (index % 13) * 3_000_000)
        # 8 scenarios carry two consecutive, separately identified reductions.
        successive = index % 5 == 0
        latest_old = current_price + (100 if currency == "UF" else 8_000_000)
        older_old = latest_old + (100 if currency == "UF" else 8_000_000)
        if currency == "UF":
            latest_new, older_new = current_price, latest_old
        else:
            latest_new, older_new = current_price, latest_old
        phone = f"+5697000{index + 1:04d}"
        lead_id = ObjectId()
        temperature = temperature_names[index % len(temperature_names)]
        drop_at = now - dt.timedelta(days=3)
        sent_at = now - dt.timedelta(days=2)
        history = [{
            "codigo": code, "campo": "precio_publicado",
            "fecha": (drop_at - dt.timedelta(days=3)).isoformat(),
            "moneda_anterior": currency, "moneda_nueva": currency,
            "monto_anterior": older_old, "monto_nuevo": older_new,
        }] if successive else []
        history.append({
            "codigo": code, "campo": "precio_publicado",
            "fecha": drop_at.isoformat(),
            "moneda_anterior": currency, "moneda_nueva": currency,
            "monto_anterior": latest_old, "monto_nuevo": latest_new,
        })
        property_doc = _property_document(
            code, currency=currency, current_price=current_price,
            commune=commune, variation=index,
        )
        db["universo_cartera_prop360"].insert_one(property_doc)
        db["universo_cartera_prop360_historial"].insert_many(history)
        inquiry_at = drop_at - dt.timedelta(days=2)
        messages = [{
            "role": "user",
            "content": f"Consulté antes de la rebaja por la propiedad código {code}.",
            "timestamp": inquiry_at.isoformat(),
            "message_id": f"synthetic-history-{index:03d}",
        }]
        campaigns = []
        if successive:
            campaigns.append(_campaign_row(
                campaign_id=f"syn_old_{index:03d}", code=code,
                currency=currency, old_price=older_old, new_price=older_new,
                sent_at=now - dt.timedelta(days=5),
                expires_at=now - dt.timedelta(days=1),
                event_id=f"syn_event_old_{index:03d}",
            ))
        campaign = _campaign_row(
            campaign_id=f"syn_new_{index:03d}", code=code,
            currency=currency, old_price=latest_old, new_price=latest_new,
            sent_at=sent_at, expires_at=now + dt.timedelta(days=5),
            event_id=f"syn_event_new_{index:03d}",
        )
        campaign.update({
            "temperature_at_prepare": temperature,
            "temperature_filter": "ALL",
            "lead_temperature_snapshot": temperature,
        })
        campaigns.append(campaign)
        db["leads"].insert_one({
            "_id": lead_id,
            "phone": phone,
            "contact_phone_normalized": phone,
            "conversation_id": f"synthetic-conversation-{index:03d}",
            "phone_is_synthetic": False,
            "lead_temperature": temperature,
            "lead_temperature_effective": temperature,
            "pipeline_stage": "NEW",
            "stage": "NEW",
            "created_at": now - dt.timedelta(days=30 + index),
            "prospecto": {"codigo": code, "operacion": "Venta", "comuna": commune},
            "messages": messages,
            "price_drop_v2": {"campaigns": campaigns, "contact_preferences": {}},
        })
        fixtures.append({
            "index": index,
            "lead_id": lead_id,
            "phone": phone,
            "temperature": temperature,
            "code": code,
            "commune": commune,
            "currency": currency,
            "old_price": latest_old,
            "new_price": current_price,
            "property_doc": property_doc,
            "successive_reductions": successive,
            "scenario": _scenario(
                profile, variant, code=code, commune=commune,
                other_commune=other_commune, currency=currency,
                old_price=latest_old, new_price=current_price,
            ),
        })

    all_turns: list[dict] = []
    mock_customer_sends: list[dict] = []
    actual_responses = 0
    suppressed_responses = 0
    contexts_verified = 0
    core_intent_events_before = db["event_log"].count_documents({"event": "INTENT_DETECTED"})
    for fixture in fixtures:
        scenario = fixture["scenario"]
        context = None
        from chatbot.price_drop_leads_v2 import active_context, record_inbound
        context = active_context(db, phone=fixture["phone"], message="hola", now=now)
        expected_context = {
            "campaign_id": f"syn_new_{fixture['index']:03d}",
            "property_code": fixture["code"],
            "currency": fixture["currency"],
            "new_price_uf": fixture["new_price"] if fixture["currency"] == "UF" else None,
            "new_price_clp": fixture["new_price"] if fixture["currency"] == "CLP" else None,
        }
        context_ok = bool(context and all(context.get(k) == v for k, v in expected_context.items()))
        contexts_verified += int(context_ok)
        turn_results = []
        for turn_index, (message, expected_label) in enumerate(scenario["turns"]):
            stamp = dt.datetime.now(dt.timezone.utc)
            provider_id = f"synthetic-inbound-{fixture['index']:03d}-{turn_index}"
            from chatbot.price_drop_campaign_classifier import classify_price_drop_reply
            direct_classification = classify_price_drop_reply(message).as_dict()
            inbound_result = record_inbound(
                db, phone=fixture["phone"], provider_message_id=provider_id,
                text=message, now=stamp,
            )
            duplicate_inbound = record_inbound(
                db, phone=fixture["phone"], provider_message_id=provider_id,
                text=message, now=stamp + dt.timedelta(seconds=1),
            )
            lead = db["leads"].find_one({"_id": fixture["lead_id"]})
            conversation_id = lead.get("conversation_id")
            job_id = queue.create_inbound_job(
                db, inbound_provider_message_id=provider_id,
                conversation_id=conversation_id, lead_id=str(fixture["lead_id"]),
                phone=fixture["phone"], text=message,
                received_at=stamp - dt.timedelta(seconds=3), max_wait_seconds=1,
            )
            duplicate_job_id = queue.create_inbound_job(
                db, inbound_provider_message_id=provider_id,
                conversation_id=conversation_id, lead_id=str(fixture["lead_id"]),
                phone=fixture["phone"], text=message,
                received_at=stamp - dt.timedelta(seconds=2), max_wait_seconds=1,
            )

            async def invoke_core(phone, text, **metadata):
                return await asyncio.to_thread(
                    core.process_user_message_sync, phone, text,
                    telemetry_context=metadata,
                )

            async def fake_wasender(phone, text):
                send_id = f"mock-wasender-{uuid.uuid4().hex[:12]}"
                mock_customer_sends.append({
                    "conversation": fixture["index"], "turn": turn_index,
                    "mock_provider_message_id": send_id,
                })
                return {"success": True, "provider_message_id": send_id,
                        "delivery_status": "accepted"}

            batch = await queue.process_one_batch(
                db, worker_id="synthetic-acceptance-worker",
                llm=invoke_core, sender=fake_wasender,
                now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2),
            )
            if not batch:
                raise RuntimeError(f"synthetic_queue_did_not_process:{fixture['index']}:{turn_index}")
            delivery = (batch.get("delivery_attempts") or [])[-1:] or [{}]
            provider_trace = (traces.get(str(batch.get("_id"))) or [{}])[-1]
            lead_after = db["leads"].find_one({"_id": fixture["lead_id"]})
            stored_messages = list(lead_after.get("messages") or [])
            inbound_message_logged = any(
                item.get("role") == "user" and item.get("content") == message
                for item in stored_messages
            )
            assistant_messages = [
                m for m in stored_messages
                if m.get("role") == "assistant"
                and m.get("batch_id") == batch.get("_id")
                and m.get("delivery_status") in {"accepted", "delivered", "read"}
            ]
            latest_reply = assistant_messages[-1].get("content") if assistant_messages else ""
            intent_event = db["event_log"].find_one(
                {"event": "INTENT_DETECTED", "conversation_id": conversation_id},
                sort=[("timestamp", -1)],
            ) or {}
            core_intent = intent_event.get("intent")
            customer_response_expected = expected_label != "BAJA"
            if customer_response_expected:
                actual_responses += int(bool(latest_reply))
            else:
                suppressed_responses += int(not latest_reply)
            observed_labels_match = direct_classification.get("label") == expected_label
            visit_requested = bool(direct_classification.get("visit_requested"))
            output_unrequested_visit = (
                not visit_requested
                and _contains_visit_invitation(latest_reply)
            )
            price_question = not direct_classification.get("alternatives_requested") and bool(re.search(
                r"\b(precio|valor|rebaj|cu[aá]nto\s+(?:baj[oó]|cuesta|vale))\b",
                message, re.I,
            ))
            correct_current_price = (
                not price_question or _price_token_present(
                    latest_reply, currency=fixture["currency"], amount=fixture["new_price"],
                )
            )
            response_codes = set(re.findall(
                r"\b(?:c[oó]digo|propiedad)\s*(?:n[uú]mero\s*)?[:#]?\s*(\d{4,6})\b",
                str(latest_reply or ""), re.I,
            ))
            correct_property = not response_codes or response_codes == {fixture["code"]}
            price_claim_checks = _verify_listing_price_claims(
                latest_reply,
                currency=fixture["currency"],
                old_price=fixture["old_price"],
                new_price=fixture["new_price"],
            )
            price_claims = [claim for claim in price_claim_checks
                            if claim["kind"] == "listing_price"]
            accurate_price_claims = all(claim["verified"] for claim in price_claim_checks)
            property_fact_mismatches = _property_numeric_fact_mismatches(
                latest_reply, variation=fixture["index"],
            )
            correct_property_facts = not property_fact_mismatches
            candidate_text = provider_trace.get("response_candidate") or ""
            has_actual_generator = bool(
                provider_trace.get("pilot_context_in_prompt")
                and candidate_text.strip()
                and provider_trace.get("model")
                and (
                    mock_deepseek
                    or provider_trace.get("provider_api_calls_observed", 0) > 0
                )
            ) if customer_response_expected else True
            naturalness_ok = bool(
                not latest_reply or (
                    2 <= len(str(latest_reply).split()) <= 150
                    and not str(latest_reply).lstrip().startswith("{")
                    and "undefined" not in str(latest_reply).casefold()
                )
            )
            continuity_ok = (
                provider_trace.get("pilot_context_in_prompt") is True
                and provider_trace.get("previous_user_turns_in_prompt", -1) >= turn_index
            ) if customer_response_expected else True
            response_present_ok = bool(latest_reply) == customer_response_expected
            core_visit_intent_ok = (
                visit_requested or core_intent != "agendar_visita"
                or not customer_response_expected
            )
            core_contact_intent_ok = (
                not direct_classification.get("do_not_call")
                or core_intent != "contacto_directo"
                or not customer_response_expected
            )
            if visit_requested and customer_response_expected:
                core_visit_intent_ok = core_intent == "agendar_visita"
            if direct_classification.get("human_requested") and not direct_classification.get("do_not_call") and customer_response_expected:
                core_contact_intent_ok = core_intent == "contacto_directo"
            final_response_semantics = _campaign_final_response_checks(
                message, latest_reply, direct_classification,
                original_commune=fixture["commune"],
                property_doc=fixture["property_doc"],
            )
            queue_ok = batch.get("state") == queue.ST_RESPONDED
            duplicate_ok = (
                duplicate_inbound.get("status") == "DUPLICATE_IGNORED"
                and duplicate_job_id == job_id
            )
            campaign_after = (lead_after.get("price_drop_v2") or {}).get("campaigns") or []
            reply_events = (lead_after.get("price_drop_v2") or {}).get("reply_events") or []
            campaign_reply_event_logged = any(
                item.get("campaign_id") == f"syn_new_{fixture['index']:03d}"
                and item.get("provider_message_id") == provider_id
                for item in reply_events
            )
            temperature_snapshot = next(
                (c.get("temperature_at_prepare") for c in campaign_after
                 if c.get("campaign_id") == "syn_new_%03d" % fixture["index"]),
                None,
            )
            temperature_preserved = temperature_snapshot == fixture["temperature"]
            issues = []
            if not context_ok: issues.append("verified_campaign_context_mismatch")
            if not observed_labels_match: issues.append("classifier_expected_label_mismatch")
            if inbound_result.get("status") != "ATTRIBUTED" and expected_label != "BAJA":
                issues.append("campaign_reply_not_attributed")
            if duplicate_inbound.get("status") != "DUPLICATE_IGNORED":
                issues.append("inbound_idempotency_failed")
            if not duplicate_ok: issues.append("queue_job_idempotency_failed")
            if not queue_ok: issues.append("queue_delivery_not_terminal")
            if not response_present_ok: issues.append("response_suppression_mismatch")
            if customer_response_expected and not has_actual_generator:
                issues.append("actual_deepseek_generation_missing_or_failed")
            if customer_response_expected and not provider_trace.get("pilot_context_in_prompt"):
                issues.append("real_v2_context_missing_from_generator_prompt")
            if not correct_property: issues.append("property_identity_mismatch")
            if not correct_property_facts: issues.append("property_numeric_fact_mismatch")
            if not correct_current_price: issues.append("current_price_not_exact_on_price_question")
            if not accurate_price_claims: issues.append("price_claim_not_verified_against_latest_drop")
            if output_unrequested_visit: issues.append("visit_proposed_without_customer_request")
            if not naturalness_ok: issues.append("response_naturalness_or_length_guard_failed")
            if not continuity_ok: issues.append("multi_turn_context_discontinuity")
            if not core_visit_intent_ok: issues.append("core_visit_intent_false_positive_or_missed_request")
            if not core_contact_intent_ok: issues.append("core_contact_intent_false_positive_or_missed_request")
            if not temperature_preserved: issues.append("campaign_temperature_snapshot_changed")
            if customer_response_expected and not inbound_message_logged:
                issues.append("customer_inbound_message_not_logged")
            if inbound_result.get("status") == "ATTRIBUTED" and not campaign_reply_event_logged:
                issues.append("price_drop_v2_reply_event_not_logged")
            for check, passed in final_response_semantics.items():
                if check != "PASS" and not passed:
                    issues.append(f"final_response_semantics:{check}")
            turn_results.append({
                "turn": turn_index + 1,
                "input": message,
                "expected_classification": expected_label,
                "obtained_classification": direct_classification.get("label"),
                "classification_details": direct_classification,
                "core_intent_detected": core_intent,
                "core_intent_guard_check": "PASS" if core_visit_intent_ok and core_contact_intent_ok else "FAIL",
                "record_inbound_status": inbound_result.get("status"),
                "duplicate_inbound_status": duplicate_inbound.get("status"),
                "queue_state": batch.get("state"),
                "queue_terminal_error": batch.get("last_error"),
                "queue_job_idempotent": duplicate_ok,
                "real_deepseek_candidate": candidate_text,
                "customer_visible_reply": latest_reply,
                "customer_inbound_message_logged": inbound_message_logged,
                "price_drop_v2_reply_event_logged": campaign_reply_event_logged,
                "outbound_message_logged": bool(latest_reply and assistant_messages),
                "final_response_semantics": final_response_semantics,
                "requested_communes": _requested_communes(message),
                "deepseek_model": provider_trace.get("model"),
                "deepseek_provider_calls_reported": provider_trace.get("provider_calls_reported"),
                "deepseek_provider_api_calls_observed": provider_trace.get("provider_api_calls_observed", 0),
                "deepseek_error": provider_trace.get("provider_error"),
                "structured_generation_status": provider_trace.get("structured_generation_status"),
                "campaign_context_in_actual_prompt": provider_trace.get("pilot_context_in_prompt"),
                "previous_user_turns_in_prompt": provider_trace.get("previous_user_turns_in_prompt"),
                "expected_property_code": fixture["code"],
                "response_property_code_check": "PASS" if correct_property else "FAIL",
                "property_fact_mismatches": property_fact_mismatches,
                "currency": fixture["currency"],
                "old_price": fixture["old_price"],
                "current_price": fixture["new_price"],
                "price_verified_in_context": context_ok,
                "asked_current_price": price_question,
                "exact_current_price_in_reply": correct_current_price,
                "price_claims_verified": accurate_price_claims,
                "listing_price_claims": price_claims,
                "visit_request_in_this_turn": visit_requested,
                "visit_offer_without_request": output_unrequested_visit,
                "naturalness_heuristic": "PASS" if naturalness_ok else "FAIL",
                "continuity_check": "PASS" if continuity_ok else "FAIL",
                "reply_expected": customer_response_expected,
                "reply_present": bool(latest_reply),
                "temperature_at_prepare": temperature_snapshot,
                "temperature_at_end": lead_after.get("lead_temperature_effective"),
                "temperature_snapshot_preserved": temperature_preserved,
                "issues": issues,
            })
            all_turns.append(turn_results[-1])
        fixture["turn_results"] = turn_results
        fixture["expected_campaign_id"] = f"syn_new_{fixture['index']:03d}"
        fixture["context_verified"] = context_ok

    # Explicitly exercise both administrator notice state machines with a
    # fake sink. These functions are never allowed to pick a default sender.
    from chatbot import lead_router
    lead_router.build_secure_crm_url = lambda lead: f"https://crm.example.invalid/leads/{lead['_id']}"
    from chatbot import price_drop_leads_v2 as v2
    os.environ[v2.ADMIN_PHONE_FLAG] = v2.APPROVED_ADMIN_PHONE
    os.environ[v2.REAL_ADMIN_NOTICE_FLAG] = "true"
    mock_admin_sends: list[dict] = []
    def fake_admin_sender(phone, text):
        mock_admin_sends.append({"target": "allowlisted_admin_mock", "text": text})
        return {"success": True, "provider_message_id": f"mock-admin-{len(mock_admin_sends)}",
                "delivery_status": "accepted"}
    positive_statuses = []
    info_statuses = []
    admin_now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
    for _ in range(args.conversations + 1):
        status = v2.send_real_admin_notice_once(db, sender=fake_admin_sender, now=admin_now)
        positive_statuses.append(status.get("status"))
        if status.get("status") == "IDLE":
            break
    for _ in range(args.conversations + 1):
        status = v2.send_real_admin_info_notice_once(db, sender=fake_admin_sender, now=admin_now)
        info_statuses.append(status.get("status"))
        if status.get("status") == "IDLE":
            break
    positive_notice_count = sum(s == "SENT" for s in positive_statuses)
    info_notice_count = sum(s == "SENT" for s in info_statuses)
    notice_content_ok = all(
        "No contactar nuevamente al cliente" in row["text"]
        for row in mock_admin_sends
        if "Aviso informativo de campaña" in row["text"]
    ) and all(
        "CRM: https://crm.example.invalid/" in row["text"]
        for row in mock_admin_sends
    )
    admin_idempotency_ok = all(s not in {"SENT"} for s in positive_statuses[positive_notice_count + 1:])
    info_idempotency_ok = all(s not in {"SENT"} for s in info_statuses[info_notice_count + 1:])
    exec_notice_frozen = all(
        c.get("notifications", {}).get("property_executive", {}).get("status")
        == "FROZEN_ADMIN_ONLY"
        for lead in db["leads"].find({})
        for c in ((lead.get("price_drop_v2") or {}).get("campaigns") or [])
    )

    temperatures = {}
    temperature_changes = []
    for fixture in fixtures:
        lead = db["leads"].find_one({"_id": fixture["lead_id"]})
        campaign = next(
            row for row in (lead.get("price_drop_v2") or {}).get("campaigns") or []
            if row.get("campaign_id") == fixture["expected_campaign_id"]
        )
        key_name = fixture["temperature"]
        temperatures.setdefault(key_name, {"count": 0, "campaign_snapshot_preserved": 0})
        temperatures[key_name]["count"] += 1
        temperatures[key_name]["campaign_snapshot_preserved"] += int(
            campaign.get("temperature_at_prepare") == key_name
        )
        final_temperature = lead.get("lead_temperature_effective")
        temperature_changes.append({
            "conversation": f"synthetic-{fixture['index']:03d}",
            "initial": fixture["temperature"],
            "final": final_temperature,
            "changed_by_synthetic_crm_intent_updates": final_temperature != fixture["temperature"],
        })

    event_counts = {
        event: db["event_log"].count_documents({"event": event})
        for event in (
            "INTENT_DETECTED", "RESPONSE_GENERATED", "RESPONSE_SENT",
            "LLM_CALL", "price_drop_executive_handoff_frozen",
        )
    }
    event_counts["INTENT_DETECTED_before"] = core_intent_events_before
    db_collections = sorted(db.list_collection_names())
    issues = [issue for turn in all_turns for issue in turn["issues"]]
    expected_response_turns = sum(bool(turn.get("reply_expected")) for turn in all_turns)
    if actual_responses < expected_response_turns:
        issues.append("real_deepseek_response_count_below_expected_floor")
    if positive_notice_count == 0:
        issues.append("positive_admin_notice_mock_not_exercised")
    informational_notice_scenario_requested = (
        not args.scenario_ids
        or any(index % 10 in {8, 9} for index in args.scenario_indices)
    )
    if info_notice_count == 0 and informational_notice_scenario_requested:
        issues.append("admin_info_notice_mock_not_exercised")
    if not notice_content_ok:
        issues.append("admin_notice_content_or_crm_link_invalid")
    if not admin_idempotency_ok or not info_idempotency_ok:
        issues.append("admin_notice_idempotency_failed")
    if not exec_notice_frozen:
        issues.append("executive_notice_not_frozen")
    if external_alert_calls:
        issues.append("unexpected_core_alert_path_invoked")
    if len(mock_customer_sends) != actual_responses:
        issues.append("mock_delivery_count_does_not_match_response_count")
    if event_counts["RESPONSE_SENT"] != len(mock_customer_sends):
        issues.append("queue_response_logging_mismatch")
    if provider_calls == 0 and not mock_deepseek:
        issues.append("no_actual_deepseek_http_calls_observed")
    if "crm_price_drop_pilot_v1" in db_collections:
        issues.append("unexpected_legacy_campaign_collection_created")

    result = {
        "schema_version": "price_drop_deepseek_acceptance_v1",
        "repository": "pgalleguil/procasa-chatbot",
        "pull_request": 161,
        "reference_commit": "ee34f2092cfd4d0fa2f28dbf0ab581cd44f710b7",
        "local_head": os.popen("git rev-parse HEAD").read().strip(),
        "executed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "generation_mode": "simulated_mock" if mock_deepseek else "real_deepseek_api",
        "provider": "Synthetic response stub" if mock_deepseek else "DeepSeek real API",
        "provider_host": "not_contacted" if mock_deepseek else urlparse(base_url).hostname,
        "provider_model": "synthetic-response-stub" if mock_deepseek else config.Config.DEEPSEEK_MODEL_REASONER,
        "actual_deepseek_http_calls": provider_calls,
        "conversation_count": len(fixtures),
        "targeted_run": bool(args.scenario_ids),
        "targeted_scenario_ids": [f"synthetic-{index:03d}" for index in args.scenario_indices]
        if args.scenario_ids else [],
        "inbound_turn_count": len(all_turns),
        "conversations_with_multiple_inbound_turns": sum(len(x["turn_results"]) >= 2 for x in fixtures),
        "real_deepseek_customer_replies": 0 if mock_deepseek else actual_responses,
        "simulated_customer_replies": actual_responses if mock_deepseek else 0,
        "intentionally_suppressed_baja_turns": suppressed_responses,
        "consecutive_price_drop_scenarios": sum(x["successive_reductions"] for x in fixtures),
        "temperature_distribution": temperatures,
        "temperature_changes": temperature_changes,
        "campaign_contexts_verified": contexts_verified,
        "mocked_customer_provider_sends": len(mock_customer_sends),
        "mocked_admin_notice_sends": positive_notice_count,
        "mocked_admin_info_notice_sends": info_notice_count,
        "admin_notice_content_valid": notice_content_ok,
        "admin_notice_idempotency": admin_idempotency_ok and info_idempotency_ok,
        "executive_notices_frozen": exec_notice_frozen,
        "event_counts": event_counts,
        "isolated_database": "mongomock in-memory URLS",
        "database_collections": db_collections,
        "safety": {
            "production_mongo_uri_loaded": False,
            "production_mongo_connection_attempted": False,
            "wasender_credentials_loaded": False,
            "real_whatsapp_calls": 0,
            "real_admin_whatsapp_calls": 0,
            "deployment_performed": False,
            "pull_request_merged": False,
            "only_external_api": (
                "none; synthetic DeepSeek responses" if mock_deepseek
                else "api.deepseek.com with synthetic prompts"
            ),
            "customer_phone_fields_removed_from_model_prompt": True,
        },
        "conversation_matrix": [{
            "conversation": f"synthetic-{fixture['index']:03d}",
            "temperature": fixture["temperature"],
            "currency": fixture["currency"],
            "property_code": fixture["code"],
            "commune": fixture["commune"],
            "old_price": fixture["old_price"],
            "current_price": fixture["new_price"],
            "successive_reductions": fixture["successive_reductions"],
            "context_verified": fixture["context_verified"],
            "turns": fixture["turn_results"],
        } for fixture in fixtures],
        "issues": sorted(set(issues)),
        "status": (
            "TARGETED_SIMULATED" if not issues and mock_deepseek and args.scenario_ids else
            "SIMULATED" if not issues and mock_deepseek else
            "TARGETED_PASS" if not issues and args.scenario_ids else
            "PASS" if not issues and len(fixtures) >= 40 else "FAIL"
        ),
    }
    result["summary"] = {
        "property_accuracy_passes": sum(t["response_property_code_check"] == "PASS" for t in all_turns),
        "property_numeric_facts_passes": sum(not t["property_fact_mismatches"] for t in all_turns),
        "price_accuracy_passes": sum(t["price_claims_verified"] and t["exact_current_price_in_reply"] for t in all_turns),
        "naturalness_heuristic_passes": sum(t["naturalness_heuristic"] == "PASS" for t in all_turns),
        "final_response_relevance_passes": sum(
            (t.get("final_response_semantics") or {}).get("PASS") is True for t in all_turns
        ),
        "alternative_responses_passes": sum(
            (t.get("final_response_semantics") or {}).get("ALTERNATIVE_REQUEST_ANSWERED") is True
            and (t.get("final_response_semantics") or {}).get("ALTERNATIVE_TARGETS_ADDRESSED") is True
            and (t.get("final_response_semantics") or {}).get("NO_ORIGINAL_COMMUNE_SUBSTITUTION") is True
            for t in all_turns
        ),
        "offer_routing_passes": sum(
            (t.get("final_response_semantics") or {}).get("OFFER_HANDLER_ANSWERED") is True
            for t in all_turns
        ),
        "continuity_passes": sum(t["continuity_check"] == "PASS" for t in all_turns),
        "classification_passes": sum(t["expected_classification"] == t["obtained_classification"] for t in all_turns),
        "visit_offer_guard_passes": sum(not t["visit_offer_without_request"] for t in all_turns),
        "record_inbound_idempotency_passes": sum(t["duplicate_inbound_status"] == "DUPLICATE_IGNORED" for t in all_turns),
        "queue_idempotency_passes": sum(t["queue_job_idempotent"] for t in all_turns),
        "queue_logging_passes": event_counts["RESPONSE_SENT"] == len(mock_customer_sends),
        "customer_inbound_message_log_passes": sum(
            t.get("customer_inbound_message_logged") is True for t in all_turns
        ),
        "price_drop_v2_reply_event_log_passes": sum(
            t.get("price_drop_v2_reply_event_logged") is True
            for t in all_turns if t.get("record_inbound_status") == "ATTRIBUTED"
        ),
        "outbound_message_log_passes": sum(
            t.get("outbound_message_logged") is True for t in all_turns
        ),
        "temperature_snapshots_passes": sum(t["temperature_snapshot_preserved"] for t in all_turns),
        "structured_json_responses": sum(t["structured_generation_status"] == "STRUCTURED_JSON" for t in all_turns),
        "model_fallback_responses_after_structured_error": sum(
            t["structured_generation_status"] == "MODEL_FALLBACK_AFTER_STRUCTURED_ERROR"
            for t in all_turns
        ),
        "temperature_changes_from_synthetic_crm_intent_updates": sum(
            row["changed_by_synthetic_crm_intent_updates"] for row in temperature_changes
        ),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV,
                        help="private .env source; only DeepSeek key/endpoint are read")
    parser.add_argument("--conversations", type=int, default=40)
    parser.add_argument("--scenario-ids", nargs="+", metavar="synthetic-NNN",
                        help="run selected synthetic conversations for focused regression checks")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports" / "price_drop_deepseek_acceptance_latest.json")
    parser.add_argument("--markdown-output", type=Path,
                        help="optional Markdown summary and full per-turn matrix")
    parser.add_argument("--recheck-existing", type=Path,
                        help="re-evaluate a saved JSON matrix without calling DeepSeek or app services")
    parser.add_argument("--calls-limit", type=int, default=220,
                        help="hard stop before a DeepSeek request beyond this count")
    parser.add_argument("--mock-deepseek", action="store_true",
                        help="use local deterministic replies; never counts as DeepSeek acceptance")
    args = parser.parse_args()
    minimum_conversations = 10 if args.mock_deepseek else 40
    if args.scenario_ids:
        indices = []
        for scenario_id in args.scenario_ids:
            match = re.fullmatch(r"synthetic-(\d{3})", scenario_id)
            if not match or not 0 <= int(match.group(1)) < 40:
                parser.error(f"invalid --scenario-ids value: {scenario_id}")
            indices.append(int(match.group(1)))
        if len(set(indices)) != len(indices):
            parser.error("--scenario-ids must be unique")
        args.scenario_indices = indices
    else:
        if args.conversations < minimum_conversations:
            parser.error(f"--conversations must be at least {minimum_conversations}")
        args.scenario_indices = list(range(args.conversations))
    if args.calls_limit < 1:
        parser.error("--calls-limit must be positive")
    if args.recheck_existing:
        if not args.recheck_existing.is_file():
            parser.error("saved matrix to recheck does not exist")
        try:
            report = _reevaluate_saved_matrix(json.loads(
                args.recheck_existing.read_text(encoding="utf-8")
            ))
        except Exception as exc:
            print(f"Matrix recheck failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown_output = args.markdown_output or args.output.with_suffix(".md")
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        markdown_output.write_text(_render_markdown(report), encoding="utf-8")
        print(json.dumps({
            "status": report["status"],
            "report": str(args.output.resolve()),
            "markdown_report": str(markdown_output.resolve()),
            "replayed_turns": report["evaluation_replay"]["replayed_turns"],
            "regenerated_model_replies": 0,
            "issues": report["issues"],
        }, ensure_ascii=False, indent=2))
        return 0 if report["status"] in {
            "PASS", "SIMULATED", "TARGETED_PASS", "TARGETED_SIMULATED",
        } else 1
    if not args.mock_deepseek and not args.env_file.is_file():
        parser.error("private DeepSeek env file is unavailable")
    # The SDK call wrapper enforces an upper request limit without disclosing
    # the credential. Configure before importing the app.
    os.environ["PRICE_DROP_ACCEPTANCE_CALLS_LIMIT"] = str(args.calls_limit)
    old_create = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            report = asyncio.run(_async_main(args))
    except Exception as exc:
        print(f"Acceptance blocked/failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_output = args.markdown_output or args.output.with_suffix(".md")
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.write_text(_render_markdown(report), encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "report": str(args.output.resolve()),
        "markdown_report": str(markdown_output.resolve()),
        "conversations": report["conversation_count"],
        "inbound_turns": report["inbound_turn_count"],
        "deepseek_replies": report["real_deepseek_customer_replies"],
        "simulated_replies": report["simulated_customer_replies"],
        "deepseek_http_calls": report["actual_deepseek_http_calls"],
        "issues": report["issues"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {
        "PASS", "SIMULATED", "TARGETED_PASS", "TARGETED_SIMULATED",
    } else 1


def _render_markdown(report: dict) -> str:
    summary = report.get("summary") or {}
    rows = [
        f"# Acceptance run: {report.get('status')}",
        "",
        f"- Mode: `{report.get('generation_mode')}` ({report.get('provider')})",
        f"- Commit: `{report.get('local_head')}`",
        f"- Conversations / inbound turns: {report.get('conversation_count')} / {report.get('inbound_turn_count')}",
        f"- Targeted scenarios: {', '.join(report.get('targeted_scenario_ids') or []) or 'none'}",
        f"- Real DeepSeek replies: {report.get('real_deepseek_customer_replies')}",
        f"- Mock customer/admin deliveries: {report.get('mocked_customer_provider_sends')} / {report.get('mocked_admin_notice_sends')}",
        f"- Safety: Mongo URI loaded `{report.get('safety', {}).get('production_mongo_uri_loaded')}`, real WhatsApp calls `{report.get('safety', {}).get('real_whatsapp_calls')}`, merged `{report.get('safety', {}).get('pull_request_merged')}`",
        "",
        "## Evaluated checks",
        "",
        "| Check | Passed |",
        "|---|---:|",
    ]
    labels = {
        "property_accuracy_passes": "Property identity",
        "property_numeric_facts_passes": "Numeric property facts",
        "price_accuracy_passes": "Price claims (exact currency/current price on price questions)",
        "naturalness_heuristic_passes": "Naturalness heuristic (format/length only)",
        "final_response_relevance_passes": "Final response semantic relevance",
        "alternative_responses_passes": "Alternative request and commune relevance",
        "offer_routing_passes": "Offer routing answered",
        "continuity_passes": "V2 context and multi-turn continuity",
        "classification_passes": "Campaign classification",
        "visit_offer_guard_passes": "No unsolicited visit offer",
        "record_inbound_idempotency_passes": "Inbound idempotency",
        "queue_idempotency_passes": "Queue idempotency",
        "temperature_snapshots_passes": "Prepared temperature snapshot",
        "customer_inbound_message_log_passes": "Customer inbound message log",
        "price_drop_v2_reply_event_log_passes": "V2 campaign reply event audit log",
        "outbound_message_log_passes": "Customer-facing outbound message log",
        "structured_json_responses": "Structured model responses",
        "model_fallback_responses_after_structured_error": "Model fallback after structured error",
    }
    for key, label in labels.items():
        rows.append(f"| {label} | {summary.get(key, 0)} |")
    rows += ["", "## Issues", ""]
    issues = report.get("issues") or []
    rows.extend([f"- `{issue}`" for issue in issues] if issues else ["None."])
    rows += ["", "## Conversation matrix", ""]
    for conversation in report.get("conversation_matrix") or []:
        rows.append(
            f"### {conversation['conversation']} — {conversation['temperature']}, "
            f"{conversation['currency']} {conversation['current_price']}, "
            f"property {conversation['property_code']}"
        )
        rows += [
            "",
            "| Turn | Input | Expected / obtained | Core intent | Reply | Checks |",
            "|---:|---|---|---|---|---|",
        ]
        for turn in conversation.get("turns") or []:
            escape = lambda value: str(value or "").replace("|", "\\|").replace("\n", " ")
            checks = ", ".join(turn.get("issues") or []) or "PASS"
            rows.append(
                f"| {turn['turn']} | {escape(turn['input'])} | "
                f"{escape(turn['expected_classification'])} / {escape(turn['obtained_classification'])} | "
                f"{escape(turn.get('core_intent_detected'))} | "
                f"{escape(turn.get('customer_visible_reply'))} | {escape(checks)} |"
            )
        rows.append("")
    return "\n".join(rows).rstrip() + "\n"


async def _async_main(args):
    # Setup is synchronous; the function is async only to keep the worker
    # loop available for the queue's real coroutine-based process_one_batch.
    return await _synthetic_test_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
