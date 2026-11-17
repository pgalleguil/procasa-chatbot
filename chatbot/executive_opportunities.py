"""Persistent, evidence-backed opportunity ledger for assigned executive chats.

No sends, CRM actions, lead temperature changes, or historical cross-cycle
interpretation. The worker persists the ledger atomically with its snapshot.
Do not put customer message text, phone numbers or PII in ledger entries.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import re

from .executive_conversation_intelligence import EXECUTIVE_CASES
from .executive_semantics import intent as rule_intent, plain

ACTIONABLE = frozenset({
    "ASK_VISIT", "ASK_CONTACT", "GIVE_OFFER", "INTERESTED",
    "ASK_INFO", "ASK_ALTERNATIVES", "SEARCH_UPDATED",
    "OWNER_SELL", "OWNER_RENT", "MIXED_NEEDS",
})
EXPLICIT_REQUESTS = frozenset({"ASK_VISIT", "ASK_CONTACT", "GIVE_OFFER", "ASK_INFO", "ASK_ALTERNATIVES"})
OPEN = frozenset({"DETECTADA", "PENDIENTE_GESTION", "EN_GESTION"})
CLOSED = frozenset({"CONCRETADA", "DESCARTADA"})
CANCEL_VISIT = re.compile(
    r"(?:(?:ya )?no quiero (?:visitar|la visita)|"
    r"(?:por favor )?cancel(?:a|ar|en|emos) (?:la|mi) visita)"
)
CANCEL_CONTACT = re.compile(r"(?:ya )?no quiero que me llamen|cancela(?:r|en)? la llamada")


def _utc(text):
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _identity(cycle_id, session_id, message_id, kind):
    value = "\x1f".join((str(cycle_id), str(session_id), str(message_id), kind))
    return sha256(value.encode("utf-8")).hexdigest()[:32]


def _ordered(messages):
    # The worker already checks session, owner and assignment cycle. Never use
    # webhook arrival order for causal business interpretations.
    return sorted(
        (m for m in messages if isinstance(m, dict) and _utc(m.get("timestamp")) is not None),
        key=lambda m: (_utc(m["timestamp"]), str(m.get("message_id") or "")),
    )


def merge_opportunities(messages, existing, summary, *, cycle_id, session_id, owner_id):
    """Return the entire durable ledger plus a current-cycle active case.

    A greeting cannot erase an opportunity. A message replay cannot duplicate
    one. Agent acknowledgement is observed but is NOT formal CRM management.
    Closed cases are never reopened by reprocessing an older message.
    """
    ledger = deepcopy(existing) if isinstance(existing, list) else []
    by_id = {x.get("opportunity_id"): x for x in ledger
             if isinstance(x, dict) and isinstance(x.get("opportunity_id"), str)}
    prior_agent = None
    turns = _ordered(messages)
    last_customer = next((m for m in reversed(turns)
                          if m.get("actor_type") == "customer" and not m.get("_context_only")), None)
    last_customer_id = (last_customer or {}).get("provider_message_id")
    accepted_model = summary.get("decision_basis") in ("verified_model", "grounded_model")
    if summary.get("verification") in ("rejected", "failed", "unavailable"):
        accepted_model = False
    if summary.get("decision_basis") == "abstained":
        accepted_model = False

    for m in turns:
        role = m.get("actor_type")
        if role == "human_agent":
            prior_agent = m
            if m.get("_context_only"):
                continue
            timestamp = _utc(m.get("timestamp"))
            # A WhatsApp reply is evidence of contact, never proof of an
            # actual visit, offer acceptance or SLA management.
            for item in ledger:
                if not isinstance(item, dict) or item.get("status") not in OPEN:
                    continue
                if (str(item.get("assignment_cycle_id")) != str(cycle_id)
                        or str(item.get("wasender_session_id")) != str(session_id)
                        or str(item.get("executive_id")) != str(owner_id)):
                    continue
                created = _utc(item.get("created_at"))
                if created is None or created >= timestamp:
                    continue
                previous_reply = _utc(item.get("agent_reply_at"))
                if previous_reply is None or previous_reply < timestamp:
                    item["agent_reply_at"] = m["timestamp"]
                    item["agent_reply_message_id"] = m.get("provider_message_id")
                    item["agent_reply_observed"] = True
            continue
        if role != "customer" or m.get("_context_only"):
            continue
        message_id = m.get("provider_message_id")
        if not isinstance(message_id, str) or not message_id.strip():
            continue
        stamp = _utc(m.get("timestamp"))
        if stamp is None:
            continue
        content = str(m.get("content") or "")
        previous_agent = ""
        if prior_agent:
            last_agent_stamp = _utc(prior_agent.get("timestamp"))
            if last_agent_stamp is not None and 0 < stamp - last_agent_stamp <= 48 * 3600:
                previous_agent = str(prior_agent.get("content") or "")
        kind = rule_intent(content, previous_agent)
        # The final customer decision can be model-derived only when verified
        # by the existing evidence safeguards in the hybrid classifier.
        if m is last_customer and accepted_model and message_id in summary.get("intent_evidence_provider_ids", []):
            if summary.get("intent") in ACTIONABLE:
                kind = summary["intent"]
        if m is last_customer and summary.get("decision_basis") == "abstained":
            kind = "NO_SIGNAL"

        cancellation = plain(content).strip(" ¿?¡!.,:")
        if "?" not in content:
            cancel_type = ("ASK_VISIT" if CANCEL_VISIT.fullmatch(cancellation)
                           else "ASK_CONTACT" if CANCEL_CONTACT.fullmatch(cancellation) else None)
            if cancel_type:
                targets = [
                    x for x in ledger
                    if isinstance(x, dict) and x.get("status") in OPEN
                    and x.get("intent") == cancel_type
                    and str(x.get("assignment_cycle_id")) == str(cycle_id)
                    and str(x.get("wasender_session_id")) == str(session_id)
                    and str(x.get("executive_id")) == str(owner_id)
                    and _utc(x.get("created_at")) is not None
                    and _utc(x["created_at"]) <= stamp
                ]
                if targets:
                    target = max(targets, key=lambda x: _utc(x["created_at"]))
                    target["status"] = "DESCARTADA"
                    target["closed_reason"] = "explicit_customer_withdrawal"
                    target["closed_evidence_message_id"] = message_id
                    target["closed_at"] = m["timestamp"]
                continue

        if kind not in ACTIONABLE:
            continue
        case_type, action = EXECUTIVE_CASES[kind]
        # An ongoing case should collect evidence from repeat messages instead
        # of creating the same opportunity repeatedly.
        open_same = [
            x for x in ledger
            if isinstance(x, dict) and x.get("status") in OPEN
            and x.get("intent") == kind
            and str(x.get("assignment_cycle_id")) == str(cycle_id)
            and str(x.get("wasender_session_id")) == str(session_id)
            and str(x.get("executive_id")) == str(owner_id)
            and _utc(x.get("created_at")) is not None
            and _utc(x["created_at"]) <= stamp
        ]
        entry_id = _identity(cycle_id, session_id, message_id, kind)
        if entry_id in by_id:
            continue
        if open_same:
            target = max(open_same, key=lambda x: _utc(x["created_at"]))
            # Include the message ID only once; don't create unbounded reply
            # evidence inside one entry (canonical messages remain available).
            ids = target.setdefault("evidence_message_ids", [])
            if message_id not in ids:
                ids.append(message_id)
            target["last_customer_at"] = m["timestamp"]
            target["last_customer_message_id"] = message_id
            # For replay stability register the correlation key on the case.
            aliases = target.setdefault("correlation_ids", [])
            if entry_id not in aliases:
                aliases.append(entry_id)
                by_id[entry_id] = target
            continue
        item = {
            "opportunity_id": entry_id,
            "assignment_cycle_id": str(cycle_id),
            "wasender_session_id": str(session_id),
            "executive_id": str(owner_id),
            "case_type": case_type,
            "intent": kind,
            "status": "PENDIENTE_GESTION" if kind in EXPLICIT_REQUESTS else "DETECTADA",
            "next_step_hint": action,
            "created_at": m["timestamp"],
            "last_customer_at": m["timestamp"],
            "last_customer_message_id": message_id,
            "evidence_message_ids": [message_id],
            "agent_reply_observed": False,
            "formal_crm_management": False,
            "evidence_kind": "verified_model" if m is last_customer and accepted_model and kind == summary.get("intent") else "customer_rule",
        }
        ledger.append(item)
        by_id[entry_id] = item
    active = [
        x for x in ledger
        if isinstance(x, dict) and x.get("status") in OPEN
        and str(x.get("assignment_cycle_id")) == str(cycle_id)
        and str(x.get("wasender_session_id")) == str(session_id)
        and str(x.get("executive_id")) == str(owner_id)
    ]
    active.sort(key=lambda x: (_utc(x.get("last_customer_at")) or 0, x.get("opportunity_id", "")))
    return ledger, (active[-1] if active else None), len(active)
