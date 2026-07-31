"""Deterministic safeguards for outbound chatbot conversation behaviour."""
from __future__ import annotations

import re


_PHONE_REQUEST = re.compile(
    r"(?:ind[ií]came|necesito|d[eé]jame|comparte|dame|p[aá]same|"
    r"(?:me\s+)?puedes?\s+(?:compartir|dar|pasar)|cu[aá]l\s+es)"
    r".{0,80}(?:tu\s+)?(?:tel[eé]fono|n[uú]mero(?:\s+celular)?|celular|whatsapp|"
    r"n[uú]mero\s+de\s+contacto)",
    re.IGNORECASE | re.DOTALL,
)


def outbound_phone_request(text: str) -> bool:
    """True only for an explicit request, never for ordinary contact mentions."""
    normalized = str(text or "").casefold()
    if re.search(r"\bno\s+(?:necesito|quiero|hace falta).{0,80}(?:tel[eé]fono|n[uú]mero|celular|whatsapp)", normalized):
        return False
    return bool(_PHONE_REQUEST.search(normalized))


def safe_phone_free_response(original: str) -> str:
    """Remove the prohibited request while retaining any useful answer text."""
    parts = re.split(r"(?<=[.!?])\s+", str(original or "").strip())
    retained = [part for part in parts if not outbound_phone_request(part)]
    useful = " ".join(retained).strip()
    follow_up = "¿Qué día o rango horario te acomoda más?"
    if not useful:
        return f"Para avanzar con la coordinación, {follow_up[0].lower()}{follow_up[1:]}"
    if "?" in useful:
        return useful
    return f"{useful} {follow_up}"


def nudge_eligibility(lead: dict) -> dict:
    """Return a decision plus auditable reason without relying on response text."""
    status = str(lead.get("conversation_status") or "")
    stage = str(lead.get("stage") or lead.get("pipeline_stage") or "").upper()
    last_intent = str(lead.get("last_intent") or "").upper()
    pending = lead.get("pending_response") or {}
    if status == "BLOCKED_EXTERNAL_BROKER":
        return {"eligible": False, "reason": "blocked_external_broker", "evidence": status, "state": status}
    if status in {"STOPPED_BY_CLIENT", "CLOSED", "HUMAN_HANDOFF"}:
        return {"eligible": False, "reason": "conversation_status", "evidence": status, "state": status}
    if stage in {"ARCHIVED", "REJECTED", "CLOSED_LOST", "CLOSED_WON", "VISIT_DONE", "VISIT_SCHEDULED"}:
        return {"eligible": False, "reason": "terminal_stage", "evidence": stage, "state": stage}
    # Intent, an executive assignment and an alert are not proof that a person
    # has taken over.  Only a canonical status/timestamp stops the nudge.
    takeover_at = lead.get("human_takeover_at") or (lead.get("lifecycle") or {}).get("human_takeover_at")
    if takeover_at:
        return {"eligible": False, "reason": "human_takeover_confirmed", "evidence": takeover_at, "state": "human_handoff"}
    if lead.get("bot_pausado") or lead.get("delivery_unknown_pending"):
        return {"eligible": False, "reason": "manual_pause_or_delivery_unknown", "evidence": True, "state": status}
    messages = lead.get("messages") or []
    last_message = messages[-1] if messages else {}
    if last_message.get("role") == "assistant" and last_message.get("tipo") in {
        "rechazo_corredor", "cierre_conversacion", "despedida",
    }:
        return {"eligible": False, "reason": "terminal_bot_message", "evidence": last_message.get("tipo"), "state": status}
    return {"eligible": True, "reason": "eligible", "evidence": None, "state": status or stage}
