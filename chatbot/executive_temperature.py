"""Advisory, evidence-linked temperature from executive/customer dialogue.

Independent of lead_temperature_effective (HOT/COLD), initial routing,
SLA, outbound WhatsApp, consent and formal CRM management.
"""
from __future__ import annotations

HOT_INTENTS = frozenset({"ASK_VISIT", "ASK_CONTACT", "GIVE_OFFER"})
WARM_INTENTS = frozenset({
    "ASK_INFO", "ASK_ALTERNATIVES", "SEARCH_UPDATED", "INTERESTED",
    "OWNER_SELL", "OWNER_RENT", "MIXED_NEEDS",
})
OPEN = frozenset({"DETECTADA", "PENDIENTE_GESTION", "EN_GESTION"})


def classify_executive_priority(summary: dict, opportunities: list | None = None,
                                active_search: dict | None = None) -> dict:
    """Return temperature and central-support action, without side effects.

    The latest message may be "gracias" or "ok": open customer requests
    remain actionable until the client withdraws or CRM closes them. An
    executive reply alone does not prove the question was answered.
    """
    summary = summary if isinstance(summary, dict) else {}
    opportunities = opportunities if isinstance(opportunities, list) else []
    active_search = active_search if isinstance(active_search, dict) else {}
    open_items = [x for x in opportunities
                  if isinstance(x, dict) and x.get("status") in OPEN]
    open_intents = {str(x.get("intent") or "") for x in open_items}
    intent = str(summary.get("intent") or "NO_SIGNAL")
    management = summary.get("executive_management") or {}
    opted_out = (summary.get("contact_permission") == "OPTED_OUT"
                 or intent == "OPT_OUT" or bool(active_search.get("opt_out")))
    search_finished = (summary.get("search_status") == "COMPLETED"
                       or active_search.get("status") == "COMPLETED"
                       or intent == "SEARCH_COMPLETED")
    if opted_out:
        temperature, action, reason = "CERRADO", "NO_CONTACTAR", "BAJA_EXPLICITA"
    elif search_finished:
        temperature, action, reason = "CERRADO", "REVISAR_CIERRE", "BUSQUEDA_FINALIZADA"
    elif open_intents & HOT_INTENTS or intent in HOT_INTENTS:
        temperature, action, reason = "CALIENTE", "PRIORIZAR_VISITA_O_CONTACTO", "SOLICITUD_ACCION"
    elif (active_search.get("status") == "ACTIVE"
          and active_search.get("followup_required")):
        temperature, action, reason = "TIBIO", "BUSCAR_ALTERNATIVAS", "BUSQUEDA_ACTIVA"
    elif open_intents & WARM_INTENTS or intent in WARM_INTENTS:
        temperature, action, reason = "TIBIO", "RESPONDER_Y_SEGUIR", "INTERES_O_CONSULTA"
    elif summary.get("needs_review") or management.get("needs_review"):
        temperature, action, reason = "REVISAR", "REVISION_MANUAL", "EVIDENCIA_AMBIGUA"
    else:
        # No customer evidence means UNKNOWN, not an artificially COLD lead.
        temperature, action, reason = "SIN_EVIDENCIA", "ESPERAR_SENAL", "SIN_SENAL_COMERCIAL"
    search_action = (active_search.get("status") == "ACTIVE"
                     and active_search.get("followup_required")
                     and not opted_out)
    evidence = [str(x.get("last_customer_message_id"))
                for x in open_items
                if x.get("last_customer_message_id")]
    last_id = summary.get("last_customer_message_id")
    if last_id and str(last_id) not in evidence:
        evidence.append(str(last_id))
    return {
        "version": "executive_priority_v1",
        "temperature": temperature,
        "reason": reason,
        "next_action": action,
        "central_support_required": bool(search_action),
        "needs_review": bool(summary.get("needs_review")),
        "evidence_provider_ids": evidence[:12],
        "classification_scope": "authorized_executive_dialogue",
        "changes_crm_sla": False,
    }
