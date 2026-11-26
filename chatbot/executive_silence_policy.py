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
STOP_INTENTS = {"OPT_OUT", "SEARCH_COMPLETE", "SEARCH_COMPLETED", "SEARCH_DEFERRED", "CONTACT_DECLINED", "CONTACT_UNRECOGNIZED", "RESPONSE_PENDING", "PROPERTY_DECLINED"}
STOP_SEARCH = {"COMPLETED", "CLOSED", "STOPPED", "INACTIVE", "SEARCH_COMPLETE", "DEFERRED"}
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


def _morning_slot(day):
    return CHILE.localize(datetime(day.year, day.month, day.day, 9), is_dst=None).astimezone(timezone.utc)


def _next_retry_slot(value: datetime, original_due: datetime) -> datetime:
    """Missed evening contacts resume at opening on the next working day."""
    local, due_local = as_chile(value), as_chile(original_due)
    day = local.date()
    if is_business_day(day):
        if day > due_local.date() and local.hour < 9:
            return _morning_slot(day)
        if local.hour < 20:
            return next_followup_slot(value)
    day += timedelta(days=1)
    while not is_business_day(day):
        day += timedelta(days=1)
    return _morning_slot(day)


def _in_followup_slot(value: datetime, due: datetime) -> bool:
    local = as_chile(value)
    if not is_business_day(local.date()) or value < due:
        return False
    if local.date() > as_chile(due).date() and 9 <= local.hour < 10:
        return True
    return any(
        hour * 60 + minute <= local.hour * 60 + local.minute
        < hour * 60 + minute + SLOT_GRACE_MINUTES
        for hour, minute in SEND_SLOTS
    )


def sequence_due(previous_accepted: datetime, attempt: int) -> datetime:
    """Stage 2: next workday at 12:30; stage 3: two workdays later at 18:00."""
    if attempt not in (2, 3):
        raise ValueError("invalid_attempt")
    day = as_chile(previous_accepted).date()
    remaining = 1 if attempt == 2 else 2
    while remaining:
        day += timedelta(days=1)
        if is_business_day(day):
            remaining -= 1
    hour, minute = (12, 30) if attempt == 2 else (18, 0)
    return CHILE.localize(datetime(day.year, day.month, day.day, hour, minute), is_dst=None).astimezone(timezone.utc)


def sequence_slot(now, due, attempt):
    """Return eligible flag and next due; recover overdue stages at 09:00."""
    local, target = as_chile(now), as_chile(due)
    day = local.date()
    if now < due:
        return False, due
    if is_business_day(day):
        minutes = local.hour * 60 + local.minute
        if day == target.date():
            end = 14 * 60 if attempt == 2 else 20 * 60
            if minutes < end:
                return True, due
        elif 9 * 60 <= minutes < 10 * 60:
            return True, _morning_slot(day)
        elif day > target.date() and minutes < 9 * 60:
            return False, _morning_slot(day)
    day += timedelta(days=1)
    while not is_business_day(day):
        day += timedelta(days=1)
    return False, _morning_slot(day)


def has_pending_commitment(lead: dict) -> bool:
    """Waiting for a customer is not a scheduled executive commitment."""
    if not (lead.get("follow_up_status") == "pending" or lead.get("follow_up_required")):
        return False
    return not (lead.get("management_status") == "managed_waiting_response"
                and lead.get("contact_attempted") is True
                and lead.get("effective_contact") is False)


def evaluate_silence(
    lead: dict, *, now: datetime, enabled_since: datetime,
    contact_authorized: bool = False, inquiry_verified: bool = False,
    history_complete: bool = False, session_verified: bool = False,
    followup_attempts: int | None = None,
    outstanding_own_action: bool | None = None,
    continuity_since: datetime | None = None,
    scheduled_contact_clear: bool = False,
    recovery_until: datetime | None = None,
    verified_followups: tuple = (),
    delayed_initial_until: datetime | None = None,
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
    if has_pending_commitment(lead):
        return _result("HOLD", "crm_followup_pending")
    if (type(followup_attempts) is not int or not 0 <= followup_attempts < 3
            or len(verified_followups) != followup_attempts
            or any(not isinstance(x, dict) or not x.get("message_id") or _time(x.get("accepted_at")) is None
                   for x in verified_followups)):
        return _result("HOLD", "attempt_already_exists_or_unknown")
    verified_ids = {x["message_id"] for x in verified_followups}
    if len(verified_ids) != followup_attempts:
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
    if (summary.get("outreach_hold") in {"SEARCH_DEFERRED", "CONTACT_DECLINED", "CONTACT_UNRECOGNIZED"}
            or summary.get("intent") in STOP_INTENTS
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
            or ci.get("last_processed_message_id") not in ({last.get("message_id")} | verified_ids)):
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
        if (msg.get("message_id") in verified_ids and msg.get("actor_type") == "automation"
                and msg.get("source") == "crm_exec_silence_api" and msg.get("automatic") is True):
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
    if verified_followups:
        if customers or scenario != "INITIAL_NO_REPLY":
            return _result("HOLD", "dialogue_policy_pending")
        previous = [_time(x["accepted_at"]) for x in verified_followups]
        if not all(anchor < t <= now for t in previous) or any(
                as_chile(a).date() >= as_chile(b).date() for a, b in zip(previous, previous[1:])):
            return _result("HOLD", "invalid_sequence_history")
        attempt = followup_attempts + 1
        due = sequence_due(previous[-1], attempt)
        allowed, next_due = sequence_slot(now, due, attempt)
        return _result("CANDIDATE" if allowed else "WAIT",
                       "customer_reply_overdue" if allowed else "sequence_wait",
                       due_at=next_due, anchor_message_id=last["message_id"], scenario=scenario, attempt=attempt)
    due = next_followup_slot(add_business_minutes(anchor, WAIT_MINUTES))
    if now < due:
        return _result("WAIT", "silence_timer", due_at=due, anchor_message_id=last["message_id"], scenario=scenario)
    # A separately authorized, same-day recovery window never waives the
    # minimum silence, expiry, contact or history checks above.
    recovery_end = _time(recovery_until)
    local_now, local_anchor = as_chile(now), as_chile(anchor)
    recovery_window = (
        recovery_end is not None and now < recovery_end
        and local_now.date() == local_anchor.date() == as_chile(recovery_end).date()
        and is_business_day(local_now.date())
        and 20 <= local_now.hour < 21
        and (as_chile(recovery_end).hour, as_chile(recovery_end).minute) == (21, 0)
    )
    # Exact incident recovery, authorized by the operator; only a first
    # attempt during business hours on the following day. All prerequisite
    # and canonical history checks above remain mandatory.
    delayed_end = _time(delayed_initial_until)
    delayed_window = (
        delayed_end is not None and now < delayed_end
        and scenario == "INITIAL_NO_REPLY" and followup_attempts == 0
        and local_now.date() == as_chile(delayed_end).date()
        and local_anchor.date() < local_now.date()
        and is_business_day(local_now.date()) and 9 <= local_now.hour < 19
    )
    if not _in_followup_slot(now, due) and not recovery_window and not delayed_window:
        return _result("WAIT", "outside_send_window",
                       due_at=_next_retry_slot(now, due),
                       anchor_message_id=last["message_id"], scenario=scenario)
    return _result("CANDIDATE", "customer_reply_overdue", due_at=due,
                   anchor_message_id=last["message_id"], scenario=scenario)
