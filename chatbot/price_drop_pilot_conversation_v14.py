"""Isolated V14 price-drop conversation candidate.

This module prepares copy and generates replies only from a caller-supplied,
verified test context. It never loads CRM records or sends messages.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

VERSION = "price_drop_pilot_natural_v14"
MAX_REPLY_CHARS = 900

_CODE = re.compile(r"^[0-9]{4,6}$")
_ACTION_WORDS = re.compile(r"\b(?:confirmad[oa]s?|agendad[oa]s?|reservad[oa]s?)\b", re.I)
_NEGATIVE_STATUS = re.compile(
    r"\b(?:no hay|no est[aá]|no se ha|no hemos|todav[ií]a no|a[uú]n no|"
    r"sin visita|ninguna visita|no qued[oó]|no he|no tengo|"
    r"no existe|no consta|no figura)\b", re.I
)
_ACTION_VERBS = re.compile(
    r"\b(?:ya\s+)?(?:he\s+)?(?:confirm[eé]|agend[eé]|reserv[eé]|"
    r"notifiqu[eé]|avis[eé]|contact[eé]|llam[eé]|registr[eé]|"
    r"ingres[eé]|guard[eé])\b", re.I
)
_CONTACT_PROMISE = re.compile(
    r"\b(?:un|el|la)\s+(?:ejecutiv[oa]|asesor[oa]|equipo comercial)\b.{0,55}"
    r"\b(?:te llamar[aá]|te contactar[aá]|te escribir[aá]|se comunicar[aá])\b", re.I
)
_UNSUPPORTED_PERSONAL_DATA = re.compile(
    r"\b(?:env[ií]ame|dame|comp[aá]rteme)\b.{0,35}"
    r"\b(?:tu rut|tu correo|tu tel[eé]fono|tu n[uú]mero)\b", re.I
)
_ADDRESS = re.compile(
    r"\b(?:calle|avenida|pasaje|transversal)\s+[a-záéíóúñ]+(?:\s+\d{1,5})?\b"
    r"|\b(?:depto\.?|dpto\.?|unidad)\s+(?:n[°º]\s*)?\d{1,4}\b"
    r"|\bdirecci[oó]n\s+(?:es|de)?\s*[^,\n]{0,35}\b\d{1,5}\b", re.I
)
_PRIOR_INQUIRY_CLAIM = re.compile(
    r"\b(?:consultaste|hab[ií]as consultado|preguntaste|nos preguntaste|"
    r"ya hab[ií]as visto|te interesaste)\b", re.I
)
_UF_VALUE = re.compile(r"\bUF\s*([0-9][0-9.,]*)", re.I)
_OWNER_NEGOTIATION_CLAIM = re.compile(
    r"\b(?:el propietario|la propietaria|el due[nñ]o|la due[nñ]a|"
    r"el vendedor|la vendedora)\b.{0,55}\b(?:acepta|est[aá] dispuesto|"
    r"est[aá] flexible|negocia|recibe ofertas)\b", re.I
)
_MEASURE = {
    "dormitorios": re.compile(r"\b([0-9]+)\s*(?:dormitorios?|piezas?)\b", re.I),
    "banos": re.compile(r"\b([0-9]+)\s*baños?\b", re.I),
    "superficie_total": re.compile(r"\b([0-9]+(?:[.,][0-9]+)?)\s*(?:m2|m²|metros cuadrados)\b", re.I),
}


class CandidateContextError(ValueError):
    """Missing or contradictory verification data blocks candidate preparation."""


def _number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise CandidateContextError(f"{name}_invalid") from exc
    if not math.isfinite(result) or result <= 0:
        raise CandidateContextError(f"{name}_invalid")
    return result


def _format_uf(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".")


def _display_name(value: Any, name: str) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > 100 or any(ch in clean for ch in "\r\n<>"):
        raise CandidateContextError(f"{name}_invalid")
    return clean


def prepare_initial_message(verified: dict[str, Any]) -> dict[str, Any]:
    """Render a preview from verified facts; it has no provider or DB effects.

    current_price_verified must refer to a live read of the property's current
    sale price. An earlier inquiry is mentioned only when both its boolean and
    a traceable evidence value are present.
    """
    code = str(verified.get("property_code") or "").strip()
    if not _CODE.fullmatch(code):
        raise CandidateContextError("property_code_invalid")
    if verified.get("current_price_verified") is not True:
        raise CandidateContextError("current_price_not_verified")
    if verified.get("available_for_sale") is not True:
        raise CandidateContextError("property_not_verified_for_sale")

    old = _number(verified.get("old_price_uf"), "old_price")
    current = _number(verified.get("current_price_uf"), "current_price")
    proposed = _number(verified.get("new_price_uf"), "new_price")
    if abs(current - proposed) > 0.01:
        raise CandidateContextError("current_price_mismatch")
    if old <= current:
        raise CandidateContextError("discount_not_verified")

    property_type = _display_name(verified.get("property_type"), "property_type").lower()
    comuna = _display_name(verified.get("comuna"), "comuna")
    supplied_url = str(verified.get("publication_url") or
                       f"https://www.procasa.cl/{code}").strip()
    parsed = urlsplit(supplied_url)
    if parsed.scheme != "https" or parsed.hostname not in {"procasa.cl", "www.procasa.cl"}:
        raise CandidateContextError("publication_url_invalid")
    if parsed.path.rstrip("/").split("/")[-1] != code or parsed.query or parsed.fragment:
        raise CandidateContextError("publication_url_mismatch")

    evidence = str(verified.get("prior_inquiry_evidence") or "").strip()
    prior_inquiry_verified = (
        verified.get("prior_inquiry_verified") is True and bool(evidence)
    )
    drop = old - current
    feminine_types = {
        "casa", "oficina", "parcela", "cabaña", "bodega", "finca",
        "propiedad", "vivienda",
    }
    feminine = property_type in feminine_types
    article = "la" if feminine else "el"
    object_pronoun = "verla" if feminine else "mirarlo"
    property_phrase = f"{article} {property_type}"
    if prior_inquiry_verified:
        opening = (
            "¡Hola! ¿Cómo estás? 😊 Te escribimos de PROCASA porque hace un tiempo "
            f"consultaste por {property_phrase} de {comuna}."
        )
        closing_question = (
            f"¿Te gustaría volver a {object_pronoun} con este nuevo precio?"
        )
    else:
        opening = (
            "¡Hola! ¿Cómo estás? 😊 Te escribimos de PROCASA para contarte que bajó "
            f"el precio de {property_phrase} de {comuna}."
        )
        closing_question = f"¿Te gustaría {object_pronoun} con este nuevo precio?"

    message = (
        f"{opening}\n\n"
        f"Te quería contar que bajó {_format_uf(drop)} UF: antes estaba en "
        f"UF {_format_uf(old)} y ahora en UF {_format_uf(current)}.\n\n"
        "Te dejo la publicación actualizada para que puedas revisarla:\n"
        f"{supplied_url}\n\n"
        f"{closing_question}"
    )
    return {
        "message": message,
        "property_code": code,
        "property_type": property_type,
        "comuna": comuna,
        "old_price_uf": old,
        "new_price_uf": current,
        "discount_uf": drop,
        "publication_url": supplied_url,
        "prior_inquiry_verified": prior_inquiry_verified,
        "prior_inquiry_evidence": evidence if prior_inquiry_verified else None,
        "preview_only": True,
    }


def _last_question(text: str) -> str | None:
    clean = str(text or "").strip()
    end = clean.rfind("?")
    if end < 0:
        return None
    start = max(clean.rfind("\n", 0, end), clean.rfind(".", 0, end),
                clean.rfind("!", 0, end))
    return clean[start + 1:end + 1].strip() or None


def _bounded_history(history: list[dict]) -> list[dict]:
    """Keep recent literal turns within a modest context budget."""
    selected: list[dict] = []
    char_count = 0
    for turn in reversed(history[-24:]):
        customer = str(turn.get("customer_text") or "").strip()[:700]
        assistant = str(turn.get("bot_text") or "").strip()[:900]
        if not customer and not assistant:
            continue
        exchange = {"customer_text": customer, "bot_text": assistant}
        cost = len(customer) + len(assistant)
        if char_count + cost > 11000:
            break
        selected.append(exchange)
        char_count += cost
    return list(reversed(selected))


def _build_messages(message: str, ctx: dict, history: list[dict]) -> list[dict]:
    from .prompts import SYSTEM_PROMPT_PROSPECTO

    outreach = str(ctx.get("outbound_message") or "").strip()
    if not outreach:
        raise CandidateContextError("exact_outbound_message_missing")
    code = str(ctx.get("property_code") or "").strip()
    if not _CODE.fullmatch(code):
        raise CandidateContextError("property_code_invalid")
    old = _number(ctx.get("old_price_uf"), "old_price")
    current = _number(ctx.get("new_price_uf"), "current_price")
    property_type = _display_name(ctx.get("property_type"), "property_type")
    facts = ctx.get("property_facts") if isinstance(ctx.get("property_facts"), dict) else {}
    comuna = str(facts.get("comuna") or ctx.get("comuna") or "").strip()
    if not comuna:
        raise CandidateContextError("comuna_missing")
    prior_inquiry_verified = (
        ctx.get("prior_inquiry_verified") is True
        and bool(str(ctx.get("prior_inquiry_evidence") or "").strip())
    )
    contact_reason = (
        "aviso de rebaja de una propiedad por la que existe una consulta previa verificada"
        if prior_inquiry_verified else
        "aviso de rebaja; no se debe afirmar que hubo una consulta previa"
    )
    recent = _bounded_history(history)
    last_assistant = (
        recent[-1].get("bot_text") if recent and recent[-1].get("bot_text")
        else outreach
    )
    pending_question = _last_question(last_assistant)
    customer_statements = [
        turn["customer_text"] for turn in recent if turn.get("customer_text")
    ]
    facts_text = ", ".join(
        f"{key}={value}" for key, value in facts.items()
        if key in {
            "dormitorios", "banos", "superficie_total", "superficie_util",
            "superficie_terraza", "bodegas", "estacionamientos", "piso",
            "orientacion", "gastos_comunes_clp", "comuna", "piscina",
            "mascotas_ficha", "parrillas_ficha",
        }
    ) or "ningún otro atributo confirmado"
    system_context = (
        "[CONTEXTO COMERCIAL DEL MENSAJE SALIENTE - PRIORIDAD]\n"
        "PROCASA inició la conversación. El cliente responde al mensaje inicial "
        "que aparece literalmente como el primer turno assistant. Sigue esa "
        "conversación; no la reinicies como una captación nueva.\n"
        f"Motivo de contacto verificado: {contact_reason}.\n"
        f"Propiedad activa verificada: {property_type} en {comuna}, código {code}.\n"
        f"Precio anterior verificado: UF {_format_uf(old)}. "
        f"Precio vigente verificado: UF {_format_uf(current)}.\n"
        f"Atributos disponibles verificados: {facts_text}.\n"
        f"Consulta previa verificada: {'sí' if prior_inquiry_verified else 'no'}.\n"
        f"Última pregunta pendiente, si existe: {pending_question or 'ninguna identificada'}.\n"
        "Preferencias y correcciones: usa solo las afirmaciones literales del cliente "
        "incluidas en el historial; no infieras preferencias.\n"
        f"Mensajes recientes del cliente: {customer_statements or 'ninguno antes del mensaje actual'}.\n"
        "Estados operativos confirmados: no hay visita confirmada; no se ha enviado "
        "contacto a una persona del equipo; no se han realizado escrituras en CRM. "
        "Una preferencia de día u hora no es una reserva.\n"
        "Responde al último mensaje de forma cálida, breve y natural. Contesta primero "
        "sus preguntas concretas, incluso si son varias. Mantén el hilo de la "
        "conversación y el contexto de que PROCASA inició el contacto. Interpreta "
        "un sí o un no según la última pregunta y lo dicho por el cliente. Distingue "
        "entre no haber visto la publicación, no querer visitar, no estar interesado "
        "y pedir no recibir más mensajes. Respeta rechazos. No propongas una visita "
        "automáticamente cuando el cliente solo expresa interés o pregunta por un "
        "dato. Ante consultas de negociación, contesta directamente y reconoce si "
        "la disposición del propietario no está verificada. No afirmes que el dueño "
        "acepta negociar sin respaldo. Si falta un atributo, di que no está confirmado. "
        "No inventes acciones, disponibilidad, precios, "
        "atributos, intereses, objeciones ni razones de abandono. No pidas datos "
        "personales. No afirmes que se notificó a alguien, que se escribió en CRM, "
        "o que una visita fue confirmada. No expongas dirección exacta, piso/unidad "
        "identificable ni teléfono. Devuelve solo el texto que recibiría el cliente."
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_PROSPECTO},
        {"role": "system", "content": system_context},
        {"role": "assistant", "content": outreach},
    ]
    for turn in recent:
        if turn.get("customer_text"):
            messages.append({"role": "user", "content": turn["customer_text"]})
        if turn.get("bot_text"):
            messages.append({"role": "assistant", "content": turn["bot_text"]})
    messages.append({"role": "user", "content": str(message or "")[:1600]})
    return messages


def _plain_number(value: str) -> float:
    if re.search(r"[.,]\d{3}$", value):
        return float(re.sub(r"[.,]", "", value))
    return float(value.replace(".", "").replace(",", "."))


def _positive_action_claim(answer: str) -> bool:
    for match in _ACTION_WORDS.finditer(answer):
        prefix = answer[max(0, match.start() - 35):match.start()]
        if not _NEGATIVE_STATUS.search(prefix):
            return True
    return bool(_ACTION_VERBS.search(answer) or _CONTACT_PROMISE.search(answer))


def _unsupported_facts(answer: str, ctx: dict) -> list[str]:
    reasons: list[str] = []
    old = _number(ctx.get("old_price_uf"), "old_price")
    current = _number(ctx.get("new_price_uf"), "current_price")
    allowed_prices = {round(old, 2), round(current, 2), round(old - current, 2)}
    for raw in _UF_VALUE.findall(answer):
        try:
            if round(_plain_number(raw), 2) not in allowed_prices:
                reasons.append("unverified_uf_price")
                break
        except ValueError:
            reasons.append("unreadable_uf_price")
            break
    facts = ctx.get("property_facts") if isinstance(ctx.get("property_facts"), dict) else {}
    for field, pattern in _MEASURE.items():
        for raw in pattern.findall(answer):
            try:
                parsed = float(raw.replace(",", "."))
            except ValueError:
                reasons.append("unreadable_property_measure")
                break
            verified = facts.get(field)
            if verified is None or abs(float(verified) - parsed) > 0.01:
                reasons.append(f"unverified_{field}")
                break
    if _ADDRESS.search(answer):
        reasons.append("sensitive_location_detail")
    if (ctx.get("owner_negotiation_verified") is not True
            and _OWNER_NEGOTIATION_CLAIM.search(answer)):
        reasons.append("unverified_owner_negotiation_position")
    if (ctx.get("prior_inquiry_verified") is not True
            or not str(ctx.get("prior_inquiry_evidence") or "").strip()):
        if _PRIOR_INQUIRY_CLAIM.search(answer):
            reasons.append("unsupported_prior_inquiry_claim")
    return list(dict.fromkeys(reasons))


def _violations(answer: str, ctx: dict) -> list[str]:
    reasons = _unsupported_facts(answer, ctx)
    if _positive_action_claim(answer):
        reasons.append("unconfirmed_operational_action")
    if _UNSUPPORTED_PERSONAL_DATA.search(answer):
        reasons.append("unnecessary_personal_data_request")
    return list(dict.fromkeys(reasons))


def _answer_text(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        return str(result.get("respuesta_bot") or "").strip()
    return ""


def _safe_fallback(message: str) -> str:
    return (
        "Disculpa, tuve un problema al preparar la respuesta. "
        "¿Me puedes repetir tu consulta?"
    )


def _safe_fragments(answer: str, ctx: dict) -> str:
    fragments = re.split(r"(?<=[.!?])\s+|\n+", answer.strip())
    kept = [part.strip() for part in fragments
            if part.strip() and not _violations(part.strip(), ctx)]
    return " ".join(kept)


def _model_name() -> str | None:
    try:
        from config import Config
        return str(Config.DEEPSEEK_MODEL_REASONER or "")
    except Exception:
        return None


def generate_reply(
    message: str,
    ctx: dict,
    history: list[dict] | None = None,
    *,
    generator: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Generate one candidate reply and perform at most one bounded repair."""
    started = time.monotonic()
    audit: dict[str, Any] = {
        "version": VERSION,
        "provider": "deepseek",
        "model": _model_name(),
        "context_sent": None,
        "original_response": None,
        "final_response": None,
        "rule_interventions": [],
        "errors": [],
        "repair_count": 0,
        "latency_ms": None,
        "usage": None,
        "cost": None,
    }
    try:
        messages = _build_messages(str(message or ""), ctx, history or [])
        audit["context_sent"] = messages
        if generator is None:
            from .grok_client import generar_respuesta_estructurada
            generator = generar_respuesta_estructurada
        operation_context = {
            "operacion": "Venta",
            "codigo": str(ctx["property_code"]),
            "tipo": str(ctx["property_type"]),
            "comuna": str((ctx.get("property_facts") or {}).get("comuna")
                          or ctx.get("comuna") or ""),
            "precio_uf": _format_uf(float(ctx["new_price_uf"])),
        }
        diagnostics: dict[str, Any] = {}
        result = generator(
            messages, operation_context, write_telemetry=False,
            diagnostics=diagnostics, retry_invalid_json=False,
        )
        audit.update({
            "provider": diagnostics.get("provider", "deepseek"),
            "model": diagnostics.get("model") or audit["model"],
            "usage": diagnostics.get("usage"),
            "cost": diagnostics.get("cost"),
            "provider_calls": int(diagnostics.get("provider_calls") or 1),
            "format_retries": int(diagnostics.get("format_retries") or 0),
            "raw_responses": diagnostics.get("raw_responses") or [],
        })
        answer = _answer_text(result)
        raw_responses = audit["raw_responses"]
        audit["original_response"] = (
            raw_responses[0] if raw_responses else answer or None
        )
        if not answer:
            audit["errors"].append(diagnostics.get("error") or "empty_or_invalid_model_response")
            audit["rule_interventions"].append("safe_generation_fallback")
            final = _safe_fallback(message)
        elif len(answer) > MAX_REPLY_CHARS:
            audit["rule_interventions"].append("reply_length_rejected")
            final = _safe_fallback(message)
        else:
            violations = _violations(answer, ctx)
            if violations:
                audit["rule_interventions"].extend(violations)
                repair_messages = [
                    *messages,
                    {"role": "assistant", "content": answer},
                    {"role": "system", "content": (
                        "Repara una sola vez tu respuesta anterior. Conserva las "
                        "partes que sí responden la pregunta y están respaldadas "
                        "por los datos entregados. Elimina toda afirmación de visita "
                        "confirmada, reserva, aviso, contacto o escritura en CRM que "
                        "no esté confirmada. No añadas una promesa nueva. Responde "
                        "al último mensaje del cliente y entrega solo la respuesta. "
                        "Corrige también estas infracciones detectadas: "
                        + ", ".join(violations)
                    )},
                ]
                repair_diagnostics: dict[str, Any] = {}
                audit["repair_count"] = 1
                try:
                    repaired = generator(
                        repair_messages, operation_context,
                        write_telemetry=False, diagnostics=repair_diagnostics,
                        retry_invalid_json=False,
                    )
                    repair_raw = repair_diagnostics.get("raw_responses") or []
                    audit["repair_original_response"] = (
                        repair_raw[0] if repair_raw else _answer_text(repaired) or None
                    )
                    audit["provider_calls"] += int(
                        repair_diagnostics.get("provider_calls") or 1
                    )
                    audit["format_retries"] += int(
                        repair_diagnostics.get("format_retries") or 0
                    )
                    audit["repair_usage"] = repair_diagnostics.get("usage")
                    repair_answer = _answer_text(repaired)
                    repair_violations = _violations(repair_answer, ctx)
                    if repair_answer and len(repair_answer) <= MAX_REPLY_CHARS and not repair_violations:
                        final = repair_answer
                        audit["rule_interventions"].append("bounded_model_repair")
                    else:
                        audit["rule_interventions"].extend(repair_violations)
                        preserved = _safe_fragments(answer, ctx)
                        if preserved:
                            final = preserved
                            audit["rule_interventions"].append("unsafe_sentence_removed")
                        else:
                            final = _safe_fallback(message)
                            audit["rule_interventions"].append("safe_generation_fallback")
                except Exception as exc:
                    audit["errors"].append(type(exc).__name__)
                    preserved = _safe_fragments(answer, ctx)
                    if preserved:
                        final = preserved
                        audit["rule_interventions"].append("unsafe_sentence_removed")
                    else:
                        final = _safe_fallback(message)
                        audit["rule_interventions"].append("safe_generation_fallback")
            else:
                final = answer
        audit["final_response"] = final
        return {"answer": final, "audit": audit}
    except Exception as exc:
        audit["errors"].append(type(exc).__name__)
        audit["rule_interventions"].append("safe_generation_fallback")
        final = _safe_fallback(message)
        audit["final_response"] = final
        return {"answer": final, "audit": audit}
    finally:
        audit["latency_ms"] = int((time.monotonic() - started) * 1000)
