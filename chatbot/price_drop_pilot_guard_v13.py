"""V13 focused price-drop conversational safety net for the isolated tester.

Fix observed V12 WhatsApp failures, not generic copy changes. This module
never accesses CRM, sends messages, assigns executives or books visits.
It composes verified facts and conversational intent before trying an LLM.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Callable

VERSION = "price_drop_pilot_v13_regression_test"

def plain(text: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", str(text or "").casefold())
                if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip()

_REMEMBER = re.compile(
    r"(?:que|cual|cual era|cual es)\s+(?:es\s+)?(?:la\s+)?"
    r"(?:propiedad|propeidad|propieda|depto|departamento)|"
    r"no (?:me )?recuerdo(?:\s+(?:que|cual)\s+(?:era|es))?|"
    r"de que (?:propiedad|depto|departamento) (?:hablamos|me hablas)", re.I,
)
_DATE = re.compile(
    r"\b(?:cuando|en que fecha|que dia|hace cuanto|desde cuando)\b"
    r".{0,55}\b(?:baj\w*|rebaj\w*|cambi\w*|reduj\w*)\b", re.I,
)
_AMOUNT = re.compile(
    r"\b(?:cuanto|cuantas|cuantos)\b.{0,45}\b(?:baj\w*|rebaj\w*|uf menos|"
    r"descont\w*|reduj\w*)\b|"
    r"\b(?:de cuanto fue la rebaja|cuanto bajo de precio)\b", re.I,
)
_NEGOTIATE = re.compile(
    r"\b(?:negoci\w*|negosi\w*|aceptan menos|acepta menos|"
    r"se puede ofertar|se puede hacer una oferta|"
    r"propietario acepta ofert\w*|contraoferta|se puede bajar mas|"
    r"precio conversable)\b", re.I,
)
_VISIT = re.compile(
    r"\b(?:visit\w*|visist\w*|visidta|vecfbrv|agendar|"
    r"conocer el departamento|ver el depto|ver el departamento|"
    r"ir a ver\w*|quiero verlo|quiero verla|coordinar una visita)\b|"
    r"\b(?:cuando|como|a que hora|que dia)\s+(?:se\s+)?puede\s+ir\s+a\b", re.I,
)
_CONTACT = re.compile(
    r"\b(?:quien\s+(?:me\s+)?(?:contact\w*|conatct\w*|llam\w*|escrib\w*)|"
    r"(?:me|que me)\s+(?:pueden?\s+|puedes\s+|van a\s+)?"
    r"(?:contact\w*|conatct\w*|llam\w*)|"
    r"hablar con (?:un |una )?(?:ejecutiv\w*|asesor\w*)|"
    r"se comunicara conmigo)\b", re.I,
)
_INTEREST = re.compile(
    r"\b(?:me interesa|estoy interesad\w*|quiero visit\w*|"
    r"quiero agendar|quiero ir a ver\w*|quiero hacer una oferta)\b", re.I,
)
_CONFIRM = re.compile(
    r"\b(?:ya quedo agend\w*|ya esta agend\w*|esta confirmad\w*|"
    r"quedo confirmad\w*|se confirmo la visita|tengo la visita)\b", re.I,
)
_NEGATIVE = re.compile(
    r"\b(?:no me interesa|no quiero (?:visitar|ir a ver|que me contacten)|"
    r"no quiero una visita|no me escriban|darme de baja|stop|"
    r"ya no me interesa|cancele la visita)\b", re.I,
)
# If the model fabricates a commercial handoff, replace it before sending.
_UNVERIFIED_PROMISE = re.compile(
    r"\b(?:te (?:contactara\w*|llamara\w*|escribira\w*|agend\w*|"
    r"reserv\w*)|"
    r"(?:el|la) ejecutiv\w* (?:asignad\w*|te contactara|te llamara)|"
    r"se pondran en contacto|"
    r"(?:voy a|vamos a|podemos|con gusto) coordin\w* (?:una )?visita|"
    r"(?:ya|quedo|esta) (?:agendad\w*|confirmad\w*)|"
    r"(?:ya|hoy) (?:agendamos|reservamos|confirmamos)|"
    r"le (?:avise|avisamos|avisare) al ejecutivo|"
    r"lo (?:derive|derivamos|dejamos registrado)|"
    r"tu solicitud (?:fue|esta|quedo) (?:enviad\w*|registrad\w*))\b",
    re.I,
)
_TIME = re.compile(
    r"\b(?:a\s+las|alas)\s+(\d{1,2})(?::([0-5]\d))?"
    r"(?:\s+(?:de|por)\s+(?:la|na)\s+(manana|tarde|noche))?\b", re.I,
)

def _price(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".")

def _fact_summary(ctx: dict) -> str:
    facts = ctx.get("property_facts") or {}
    parts = []
    if isinstance(facts.get("dormitorios"), (int, float)):
        parts.append(f"{int(facts['dormitorios'])} dormitorios")
    if isinstance(facts.get("banos"), (int, float)):
        parts.append(f"{int(facts['banos'])} baños")
    area = facts.get("superficie_total")
    if isinstance(area, (int, float)) and area > 0:
        parts.append(f"{area:g} m²")
    return ", ".join(parts)

def _preference(message: str, history: list[dict], parse_parts: Callable) -> dict:
    state: dict = {}
    for turn in history[-24:]:
        prev = plain(turn.get("customer_text") or "")
        if _NEGATIVE.search(prev):
            state = {}
            continue
        value = parse_parts(prev)
        if value:
            state.update(value)
    state.update(parse_parts(message))
    return state

def _time_review(text: str) -> tuple[str | None, str | None]:
    """(issue, formatted time). No timezone assumptions or silent 04:00 visits."""
    s = plain(text)
    m = _TIME.search(s)
    if not m:
        return None, None
    # Keep 10 or 11 as two discrete choices rather than silently dropping 11.
    if re.search(r"\b(?:a las|alas)\s+\d{1,2}(?::\d{2})?\s+o\s+(?:a las\s+)?\d{1,2}", s):
        return None, None
    hour = int(m.group(1))
    minute = int(m.group(2) or "0")
    part = m.group(3)
    if hour > 23:
        return "¿Me confirmas la hora en formato de 24 horas?", None
    if part and hour > 12:
        return "¿Me confirmas la hora en formato de 24 horas?", None
    if part == "manana":
        hour = 0 if hour == 12 else hour
    elif part in {"tarde", "noche"}:
        hour = hour if hour == 12 else hour + 12
    if 1 <= hour <= 7 and not part:
        # An unqualified "a las 4" must not become a 04:00 appointment.
        return (
            f"¿Te refieres a las {hour:02d}:{minute:02d} de la mañana o "
            f"a las {hour + 12:02d}:{minute:02d} de la tarde?"
        ), None
    if hour < 8 or hour >= 21:
        return (
            f"Entendí {hour:02d}:{minute:02d}, pero es un horario poco habitual "
            "para visitas. ¿Te acomodaría otro horario durante el día?"
        ), None
    return None, f"{hour:02d}:{minute:02d}"

def _preference_text(state: dict, time: str | None) -> str:
    day = (str(state.get("day") or "")
           .replace("miercoles", "miércoles").replace("sabado", "sábado"))
    hour = (f"a las {time}" if time else str(state.get("time") or ""))
    if day and hour:
        return f"{day} {hour}"
    return day or hour or str(state.get("period") or "")

def _visit_history(history: list[dict]) -> bool:
    # Most recent explicit refusal supersedes an older visit request.
    for turn in reversed(history[-10:]):
        customer = plain(turn.get("customer_text") or "")
        if _NEGATIVE.search(customer):
            return False
        if _VISIT.search(customer):
            return True
    last = plain(history[-1].get("bot_text") or "") if history else ""
    return bool(re.search(r"\b(?:visita|conocerlo|verlo|que dia|que horario|horario te|dia te)\b", last))

def reply_v13(message: str, ctx: dict, history: list[dict], *,
              parse_parts: Callable, notice_roles: tuple[str, ...] = ()) -> str | None:
    """Compose every important intent. Unknown messages go to the normal model."""
    s = plain(message)
    if not s or _NEGATIVE.search(s):
        return None
    # The customer may challenge our own outreach or a previous poor answer.
    if (re.search(r"\b(?:me (?:envi|escrib|mand)\w*|ustedes me escribieron)\b", s)
            and re.search(r"\b(?:informacion|no quieres|no me dices|mensaje)\b", s)):
        return (
            f"Tienes razón: fuimos nosotros quienes te escribimos. Disculpa "
            f"la confusión. Es el departamento de San Miguel (código "
            f"{ctx['property_code']}), ahora a UF {_price(ctx['new_price_uf'])}. "
            "Con gusto te aclaro lo que quieras saber."
        )

    property_question = bool(_REMEMBER.search(s))
    amount = bool(_AMOUNT.search(s))
    date = bool(_DATE.search(s))
    negotiate = bool(_NEGOTIATE.search(s))
    contact = bool(_CONTACT.search(s))
    visit = bool(_VISIT.search(s))
    interest = bool(_INTEREST.search(s))
    confirmation = bool(_CONFIRM.search(s))
    current_parts = parse_parts(s)
    active_visit = visit or _visit_history(history)
    schedule_update = bool(current_parts) and active_visit

    if re.search(r"\b(?:tan directo|muy directo|que seco|que brusco)\b", s):
        return (
            "Perdona, sonó demasiado directo 😊. Te escribimos para contarte "
            "que bajó el precio del departamento de San Miguel. ¿Alcanzaste "
            "a verlo o prefieres que te recuerde los detalles?"
        )

    if s in {"como", "como?", "como estas", "como estas?"} and not any(
        (property_question, amount, date, negotiate, contact, visit, interest)
    ):
        return "¡Muy bien, gracias! 😊 ¿Y tú? Te escribí por el departamento de San Miguel; si quieres te recuerdo cuál es."

    if not any((property_question, amount, date, negotiate, contact, visit,
                interest, confirmation, schedule_update)):
        return None

    sentences = []
    if property_question:
        facts = _fact_summary(ctx)
        detail = f" Tiene {facts}." if facts else ""
        sentences.append(
            f"Es el departamento de San Miguel, código {ctx['property_code']}, "
            f"que ahora está publicado a UF {_price(ctx['new_price_uf'])}."
            + detail
        )
        sentences.append(f"Te dejo la ficha: https://www.procasa.cl/{ctx['property_code']}")
    if amount:
        old, new = ctx["old_price_uf"], ctx["new_price_uf"]
        diff = old - new
        sentences.append(
            f"Bajó {_price(diff)} UF: de UF {_price(old)} a UF {_price(new)} "
            f"({(diff / old * 100):.2f}% menos).".replace(
                f"{(diff / old * 100):.2f}%", f"{(diff / old * 100):.2f}".replace(".", ",") + "%"
            )
        )
    if date:
        verified_date = ctx.get("price_drop_date")
        sentences.append(
            f"La rebaja figura registrada el {verified_date}."
            if verified_date else
            "No tengo verificada la fecha exacta en que se aplicó la rebaja."
        )
    if negotiate:
        if interest:
            sentences.append("¡Qué bueno que te interese!")
        sentences.append(
            f"El valor publicado es UF {_price(ctx['new_price_uf'])}. "
            "No tengo confirmado si el propietario aceptaría una negociación; "
            "eso debe revisarlo el ejecutivo."
        )

    if contact or confirmation:
        if set(notice_roles) >= {"ADMIN", "EXECUTIVE"}:
            status = "Se emitieron dos avisos internos de prueba, no a un ejecutivo real."
        elif notice_roles:
            status = "Se emitió un aviso interno de prueba, no a un ejecutivo real."
        else:
            status = "Todavía no se ha solicitado contacto a un ejecutivo real."
        sentences.append(status + " No hay visita confirmada.")
    if (visit and not confirmation) or schedule_update:
        issue, local_time = _time_review(s)
        state = _preference(s, history, parse_parts)
        if issue:
            sentences.append(issue)
        else:
            pref = _preference_text(state, local_time)
            if pref:
                sentences.append(f"Entiendo que te acomoda {pref}. "
                                 "Es una preferencia, no una visita confirmada.")
            elif visit:
                sentences.append("¿Qué día te acomodaría visitarlo? "
                                 "El equipo comercial tendría que confirmar disponibilidad.")
    elif interest and not (negotiate or property_question or amount or date or contact):
        sentences.append("¡Qué bueno! 😊 ¿Quieres que conversemos sobre una posible visita?")

    # Do not bring booking questions into a price/date-only request.
    # One concise WhatsApp response may contain multiple relevant answers.
    return " ".join(sentences) if sentences else None

def safe_outbound_v13(answer: str, customer_text: str, ctx: dict,
                      history: list[dict], *, parse_parts: Callable,
                      notice_roles: tuple[str, ...] = ()) -> str:
    """Last line of defense after BOTH deterministic and LLM replies."""
    text = str(answer or "").strip()
    if _UNVERIFIED_PROMISE.search(plain(text)):
        replacement = reply_v13(customer_text, ctx, history,
                                parse_parts=parse_parts, notice_roles=notice_roles)
        return replacement or (
            "Puedo ayudarte con la consulta, pero todavía no hay una visita "
            "confirmada ni un ejecutivo real notificado."
        )
    return text
