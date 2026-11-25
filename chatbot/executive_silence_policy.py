"""Conservative phase-1 policy for a single executive-chat silence follow-up.

Pure decision only: no database writes, network, scheduling, or SLA mutations.
The integration must prove all guard inputs from canonical records; unknown is
not permission. Never use this module alone as authorization to send.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import unicodedata

from .business_calendar import (
    add_business_minutes, calculate_business_minutes, as_chile, is_business_day,
    CHILE,
)

VERSION = "silence_followup_v3"
WAIT_MINUTES = 120
EXPIRY_MINUTES = 3000
# Local time, target slots. The last slot ends at 20:00 exclusively.
# These are pilot hypotheses, not measured PROCASA-optimal hours.
SEND_SLOTS = ((18, 0), (18, 30), (19, 0), (19, 30))
SLOT_GRACE_MINUTES = 30
OPEN = {"DETECTADA", "PENDIENTE_GESTION", "EN_GESTION"}
STOP_INTENTS = {"OPT_OUT", "SEARCH_COMPLETE", "SEARCH_COMPLETED", "PROPERTY_DECLINED"}
STOP_SEARCH = {"COMPLETED", "CLOSED", "STOPPED", "INACTIVE", "SEARCH_COMPLETE"}
# This is a narrow allowlist for ordinary contact/qualification questions, not
# an attempt to infer that every question or every last outbound expects a reply.
ASK = re.compile(
    r"\b(?:en que (?:le|te) puedo ayudar|"
    r"que (?:aspecto|informacion|dudas?) (?:te|le) (?:interesa|gustaria|necesita)|"
    r"(?:aun|todavia|sigue|sigues) (?:le |te )?(?:interesa|interesad[oa]|buscando)|"
    r"(?:pudo|pudiste|ha podido) revisar (?:la|el) (?:propiedad|departamento|casa)|"
    r"que le parecieron las caracteristicas|"
    r"que (?:le|te) parecen (?:las |los )?(?:alternativas|opciones|departamentos|casas|propiedades)|"
    r"(?:le|te) (?:sirve|acomoda) (?:esta|esa|el|la) (?:opcion|propiedad|departamento|casa)|"
    r"(?:cual|que) (?:es |seria )?(?:su|tu) presupuesto)\b"
)
OWN_ACTION = re.compile(
    r"\b(?:voy a revisar|vere que|voy a buscar|voy a consultar|"
    r"me contactare|le comento|te comento|le aviso|te aviso|"
    r"le escribo|te escribo|apenas tenga|revisare|confirmare|"
    r"quedo de|enviare|te mando|le mando|voy a llamar|"
    r"coordinar|agendar|visita|llamada|oferta|postulacion|"
    r"te llamo|le llamo)\b"
)
CUSTOMER_HOLD = re.compile(
    r"\b(?:ya (?:encontre|compre|arrende|consegui)|no (?:me interesa|estoy buscando|"
    r"quiero|me sirve|tengo presupuesto|se ni que es)|"
    r"no me (?:escrib|contact|llam)\w*|"
    r"denme de baja|elimin\w* mi numero|equivocad[oa]|"
    r"otro dia|mas adelante|yo (?:te|le) (?:aviso|escribo|llamo)|stop|"
    r"(?:escrib|contact|llam)\w* (?:manana|el lunes|el martes|el miercoles|el jueves|el viernes|el sabado|el domingo|a las|despues)|"
    r"proxim[oa] (?:lunes|martes|miercoles|jueves|viernes|semana)|estoy esperando)\b"
)
ACK = re.compile(r"^(?:(?:ok(?:ay)?|gracias|muchas gracias|perfecto|listo|entendido|dale)[ ,.!]*)+$")


def _plain(text):
    return " ".join("".join(
        c for c in unicodedata.normalize("NFKD", str(text or "").lower())
        if not unicodedata.combining(c)
    ).split())


def _time(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def _result(state, reason, **extra):
    return {"version": VERSION, "state": state, "reason": reason, **extra}


def next_followup_slot(value: datetime) -> datetime:
    """First preferred local slot at or after a minimum-silence threshold.

    Calendar is independent of send hours: waiting accrues 09–19 on
    Chilean working days; the final allowed slot can extend past 19:00.
    No midnight, weekend or holiday sends.
    """
    local = as_chile(value)
    day = local.date()
    while True:
        if is_business_day(day):
            for hour, minute in SEND_SLOTS:
                slot = CHILE.localize(datetime(day.year, day.month, day.day, hour, minute), is_dst=None)
                if slot >= local:
                    return slot.astimezone(timezone.utc)
        day += timedelta(days=1)


def _in_followup_slot(value: datetime, due: datetime) -> bool:
    local = as_chile(value)
    if not is_business_day(local.date()) or value < due:
        return False
    return any(
        hour * 60 + minute <= local.hour * 60 + local.minute
        < hour * 60 + minute + SLOT_GRACE_MINUTES
        for hour, minute in SEND_SLOTS
    )


def evaluate_silence(
    lead: dict, *, now: datetime, enabled_since: datetime,
    contact_authorized: bool = False, inquiry_verified: bool = False,
    history_complete: bool = False, session_verified: bool = False,
    followup_attempts: int | None = None,
    outstanding_own_action: bool | None = None,
    continuity_since: datetime | None = None,
    scheduled_contact_clear: bool = False,
) -> dict:
    """Return HOLD, WAIT or CANDIDATE (never a SEND instruction).

    One follow-up attempt per lead in phase 1, across assignment cycles.
    No historical catch-up; timer starts from a verified human outbound after
    the rollout cutoff. Unknown outcome / missing evidence blocks sending.
    """
    now, cutoff = _time(now), _time(enabled_since)
    if now is None or cutoff is None or now < cutoff:
        return _result("HOLD", "invalid_rollout_clock")
    if not all(x is True for x in (
            contact_authorized, inquiry_verified, history_complete, session_verified)):
        return _result("HOLD", "unverified_prerequisite")
    if scheduled_contact_clear is not True:
        return _result("HOLD", "scheduled_contact_pending_or_unknown")
    if lead.get("follow_up_status") == "pending" or lead.get("follow_up_required"):
        return _result("HOLD", "crm_followup_pending")
    if type(followup_attempts) is not int or followup_attempts != 0:
        return _result("HOLD", "attempt_already_exists_or_unknown")
    if outstanding_own_action is not False:
        return _result("HOLD", "our_action_pending_or_unknown")
    if any(lead.get(k) for k in (
        "test_scenario", "phone_is_synthetic", "contact_identity_incomplete",
        "opted_out", "do_not_contact",
    )):
        return _result("HOLD", "lead_excluded")
    if str(lead.get("stage") or "").lower() not in {"gestion", "new", "contactado"}:
        return _result("HOLD", "stage_not_eligible")
    lifecycle = lead.get("lifecycle") or {}
    ci = lead.get("commercial_intelligence") or {}
    summary = ci.get("summary") or {}
    owner = str(lifecycle.get("assigned_to_user_id") or "")
    cycle = str(lifecycle.get("current_assignment_cycle_id") or "")
    sid = str(ci.get("pending_session_id") or "")
    if not owner or not cycle or not sid or str(ci.get("last_processed_cycle_id")) != cycle:
        return _result("HOLD", "missing_current_assignment")
    if ci.get("pending") is not False or ci.get("status") != "ready":
        return _result("HOLD", "classification_not_ready")
    contact = summary.get("contact") or {}
    management = summary.get("executive_management") or {}
    if (summary.get("intent") in STOP_INTENTS
            or summary.get("search_status") in STOP_SEARCH
            or summary.get("contact_permission") in {"OPTED_OUT", "DENIED"}
            or (ci.get("active_search") or {}).get("opt_out")):
        return _result("HOLD", "customer_closed_or_declined")
    if (summary.get("needs_review") or summary.get("history_incomplete")
            or management.get("needs_review")
            or management.get("pending_customer_request")
            or contact.get("important_request_pending")
            or summary.get("followup_search_required")
            or summary.get("latest_important_request")
            or any(o.get("status") in OPEN for o in ci.get("opportunities") or []
                   if isinstance(o, dict))):
        return _result("HOLD", "unresolved_or_uncertain_opportunity")
    if contact.get("awaiting") != "CUSTOMER" or contact.get("agent_contacted") is not True:
        return _result("HOLD", "not_waiting_for_customer")
    messages = lead.get("messages") or []
    scoped = [m for m in messages if isinstance(m, dict)
              and m.get("channel") == "executive_whatsapp"
              and str(m.get("assignment_cycle_id")) == cycle
              and str(m.get("assigned_executive_id")) == owner
              and str(m.get("wasender_session_id")) == sid
              and m.get("actor_type") in {"human_agent", "customer"}]
    if not scoped or any(_time(m.get("timestamp")) is None for m in scoped):
        return _result("HOLD", "incomplete_message_history")
    scoped.sort(key=lambda m: (_time(m["timestamp"]), str(m.get("message_id") or "")))
    last = scoped[-1]
    anchor = _time(last["timestamp"])
    if (last.get("actor_type") != "human_agent"
            or str(last.get("actor_id")) != owner
            or last.get("source") not in {"whatsapp_direct", "crm_manual_api"}
            or not last.get("provider_message_id") or not last.get("message_id")
            or ci.get("last_processed_message_id") != last.get("message_id")):
        return _result("HOLD", "latest_outbound_not_verified")
    if anchor > now or anchor < cutoff:
        return _result("HOLD", "outside_prospective_rollout")
    if ci.get("silence_unparsed_event_at") is not None:
        # No text-only classifier can decide whether a voice note, photo or
        # attachment resolved a request. Require explicit human reconciliation.
        return _result("HOLD", "unparsed_commercial_event")
    continuous = _time(continuity_since)
    if continuous is None or continuous > anchor:
        return _result("HOLD", "capture_continuity_unverified")
    # Other channels can invalidate the snapshot, but are not used to infer a
    # new commercial intent. Ignore Prop360 form markers, not customer replies.
    for msg in messages:
        if not isinstance(msg, dict) or msg is last or msg.get("source") == "prop360":
            continue
        t = _time(msg.get("timestamp"))
        if msg.get("role") in {"user", "assistant", "agent"}:
            if t is None:
                return _result("HOLD", "unorderable_channel_history")
            if t >= anchor and msg.get("message_id") != last.get("message_id"):
                return _result("HOLD", "newer_or_concurrent_message")
    customers = [m for m in scoped if m.get("actor_type") == "customer"]
    # A post-reconnect conversation must have fresh evidence from both sides.
    # Never interpret pre-gap questions or acknowledgements as resolved.
    if customers and any(_time(m["timestamp"]) < continuous for m in customers):
        return _result("HOLD", "pre_connection_dialogue_unreconciled")
    if customers:
        prior = _plain(customers[-1].get("content"))
        if (not prior or any(CUSTOMER_HOLD.search(_plain(m.get("content"))) for m in customers) or ACK.fullmatch(prior)
                or not re.search(r"[a-z]", prior)):
            return _result("HOLD", "customer_closure_or_deferral")
    outbound = _plain(last.get("content"))
    if not ASK.search(outbound) or OWN_ACTION.search(outbound):
        return _result("HOLD", "no_clear_customer_reply_expected")
    if calculate_business_minutes(anchor, now) >= EXPIRY_MINUTES:
        return _result("HOLD", "stale_conversation")
    scenario = "DIALOGUE_INTERRUPTED" if customers else "INITIAL_NO_REPLY"
    due = next_followup_slot(add_business_minutes(anchor, WAIT_MINUTES))
    if now < due:
        return _result("WAIT", "silence_timer", due_at=due, anchor_message_id=last["message_id"], scenario=scenario)
    if not _in_followup_slot(now, due):
        return _result("WAIT", "outside_send_window",
                       due_at=next_followup_slot(now),
                       anchor_message_id=last["message_id"], scenario=scenario)
    return _result("CANDIDATE", "customer_reply_overdue", due_at=due,
                   anchor_message_id=last["message_id"], scenario=scenario)
