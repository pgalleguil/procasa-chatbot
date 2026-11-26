"""Separate inter-broker cooperation from end-customer demand.

A canje inquiry in Chilean real-estate brokerage is a B2B signal, not an
unsubscribe or evidence of a buyer's active search. Never infer an identity
from a surname, an agency name alone, or an agent's own outgoing message.
"""
import re
import unicodedata


def _plain(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", str(value or "").lower())
                            if not unicodedata.combining(c)).split())


def broker_signal(content):
    text = _plain(content)
    if not text or re.search(r'\b(?:no soy corredor[ae]?|no somos corredores|no trabajo como corredor[ae]?|no hacemos canje|no quiero canje|no busco canje)\b', text):
        return False
    if re.search(r'\b(?:canje|permuta|intercambio)\s+(?:de\s+)?(?:mi|nuestra|una|un|la|el|otra|otro)\s+(?:casa|departamento|depto|terreno|propiedad|auto|vehiculo|inmueble)\b', text):
        return False
    # Explicit broker self-identification, or asking about co-brokerage.
    return bool(re.search(
        r'\b(?:soy|somos|trabajo como)\s+(?:un[ao]?\s+)?(?:corredor[ae]?(?:es|as)?(?:\s+de\s+propiedades)?|broker(?:s)?|agente\s+inmobiliari[oa])\b'
        r'|\b(?:tengo|tenemos)\s+(?:un[ao]?\s+)?client[ea]\b.{0,100}\b(?:canje|compartir\s+comision|colaborar|corretaje)\b'
        r'|\b(?:realizan|realiza|hacen|hace|aceptan|acepta|trabajan|trabaja|manejan|maneja|reciben|recibe)\s+(?:con\s+)?canjes?\b'
        r'|\b(?:podemos|podriamos|quisiera|quiero)\s+(?:hacer|realizar|coordinar)\s+(?:un\s+)?canje\b'
        r'|\bcanje\s+(?:entre\s+corredores|inmobiliario|de\s+corretaje|con\s+otra\s+corredora)\b'
        r'|\b(?:comparten|compartimos|compartir)\s+(?:la\s+)?comision\b'
        r'|^\s*(?:hola[,!. ]+)?(?:canje|canjes)\s*[?!.]*$', text))


def broker_evidence(lead):
    """Read only canonical business/customer messages, never personal chats.

    Office/portal inquiries establish contact role, not executive management.
    Executive turns require the recorded owner, session, cycle and provider ID.
    A durable admin decision is retained until explicit review.
    """
    ci = lead.get("commercial_intelligence") or {}
    saved = ci.get("contact_role") or {}
    if isinstance(saved, dict) and saved.get("kind") == "BROKER_COLLABORATION":
        return saved
    audit = ci.get("audit_review") or {}
    if audit.get("contact_role") == "BROKER_COLLABORATION":
        return {"kind": "BROKER_COLLABORATION", "source": "admin_review",
                "message_id": audit.get("evidence_message_id")}
    if ci.get("excluded_noncommercial") is True:
        return None
    lc = lead.get("lifecycle") or {}
    for m in lead.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") not in {"user", "customer"}:
            continue
        if m.get("actor_type") not in {None, "customer"} or not m.get("message_id"):
            continue
        channel = m.get("channel") or ""
        source = m.get("source") or ""
        if channel == "executive_whatsapp":
            if (m.get("actor_type") != "customer" or not m.get("provider_message_id")
                    or not lc.get("current_assignment_cycle_id") or not lc.get("assigned_to_user_id")
                    or not ci.get("pending_session_id")
                    or str(m.get("assignment_cycle_id")) != str(lc["current_assignment_cycle_id"])
                    or str(m.get("assigned_executive_id")) != str(lc["assigned_to_user_id"])
                    or str(m.get("wasender_session_id")) != str(ci["pending_session_id"])):
                continue
        elif channel not in {"", "office_whatsapp", "whatsapp", "portal_form"} or source not in {
                "", "office_whatsapp", "wasender_inbound", "whatsapp_inbound", "whatsapp",
                "prop360", "portal_form", "chatbot"}:
            continue
        if broker_signal(m.get("content")):
            return {"kind": "BROKER_COLLABORATION", "source": "canonical_customer_inquiry",
                    "message_id": m["message_id"], "provider_message_id": m.get("provider_message_id"),
                    "channel": channel or "legacy_office"}
    return None


def automated_broker_hold(lead):
    return broker_evidence(lead) is not None
