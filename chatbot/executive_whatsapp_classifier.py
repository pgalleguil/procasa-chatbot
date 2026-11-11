"""Contrato de clasificación comercial sobre diálogos de ejecutivos (sin llamadas LLM).

La capa de clasificación es inerte: sólo prepara entrada y valida un JSON
producido por una futura llamada controlada a DeepSeek. Nunca envía mensajes.
"""
from __future__ import annotations

from typing import Any, Mapping

from .executive_whatsapp_audit import audit_transcript


COMMERCIAL_STATUSES = frozenset({
    "ACTIVE_INTEREST",
    "VISIT_COORDINATED",
    "FOLLOW_UP_PENDING",
    "NO_EFFECTIVE_CONTACT",
    "NO_LONGER_SEARCHING",
    "TRANSACTION_COMPLETED",
    "DO_NOT_CONTACT",
    "UNKNOWN",
})

AUDIT_SYSTEM_PROMPT = """Eres auditor de conversaciones comerciales inmobiliarias de PROCASA.
Analiza sólo las evidencias aportadas; no inventes gestiones ni intereses.
Separa siempre los actores chatbot, client y executive. El chatbot pertenece
exclusivamente al WhatsApp de oficina: no atribuyas sus mensajes al ejecutivo.
Un mensaje saliente sin respuesta no acredita contacto efectivo.
Una ausencia de mensajes no demuestra ausencia de gestión si la cobertura es
incompleta. Para conversaciones incompletas utiliza UNKNOWN y deja constancia.
Respeta la solicitud expresa de no ser contactado. No derives consentimiento
para campañas del simple hecho de haber consultado una propiedad.
La clasificación no modifica SLA, propietario vigente ni CRM.
Responde ÚNICAMENTE JSON con: commercial_status, contact_effective,
follow_up_required, opt_out, next_action, preferences, evidence_provider_keys,
uncertainty, coverage_complete. Los evidence_provider_keys deben corresponder
a claves de mensajes proporcionados en el expediente.
"""


def prepare_audit_input(
    lead: Mapping[str, Any],
    *, coverage_complete: bool = False,
    max_messages: int = 300,
) -> dict:
    """Produce input acotado; cobertura de historial NO se presume completa."""
    transcript = audit_transcript(lead, limit=max_messages)
    return {
        "system": AUDIT_SYSTEM_PROMPT,
        "lead_id": str(lead.get("_id") or ""),
        "coverage_complete": bool(coverage_complete),
        "messages": transcript,
        "required_schema": {
            "commercial_status": sorted(COMMERCIAL_STATUSES),
            "contact_effective": "boolean or null",
            "follow_up_required": "boolean or null",
            "opt_out": "boolean or null",
            "next_action": "string or null",
            "preferences": "object",
            "evidence_provider_keys": "array of message evidence keys",
            "uncertainty": "string describing gaps in evidence",
            "coverage_complete": "boolean",
        },
    }


def validate_audit_result(raw: Mapping[str, Any], prepared: Mapping[str, Any]) -> dict:
    """Rechaza etiquetas, evidencia, consentimiento o cobertura inventados."""
    if not isinstance(raw, Mapping):
        raise ValueError("audit_result_must_be_object")
    status = raw.get("commercial_status")
    if status not in COMMERCIAL_STATUSES:
        raise ValueError("invalid_commercial_status")
    for key in ("contact_effective", "follow_up_required", "opt_out"):
        if raw.get(key) not in (True, False, None):
            raise ValueError("invalid_boolean_field:" + key)
    evidence = raw.get("evidence_provider_keys")
    if not isinstance(evidence, list) or any(not isinstance(x, str) for x in evidence):
        raise ValueError("invalid_evidence_keys")
    available = {
        msg.get("evidence_key") for msg in prepared.get("messages", [])
        if msg.get("evidence_key")
    }
    if any(key not in available for key in evidence):
        raise ValueError("evidence_not_in_transcript")
    if raw.get("coverage_complete") is not prepared.get("coverage_complete"):
        raise ValueError("fabricated_coverage")
    if raw.get("opt_out") is True and not evidence:
        raise ValueError("opt_out_requires_evidence")
    if status != "UNKNOWN" and not evidence:
        raise ValueError("classification_requires_evidence")
    if not prepared.get("coverage_complete") and status == "NO_EFFECTIVE_CONTACT":
        raise ValueError("absence_not_proven_by_incomplete_coverage")
    if status == "DO_NOT_CONTACT" and raw.get("opt_out") is not True:
        raise ValueError("optout_label_requires_optout_true")
    if raw.get("opt_out") is True and status != "DO_NOT_CONTACT":
        raise ValueError("optout_requires_do_not_contact_status")
    preferences = raw.get("preferences")
    if not isinstance(preferences, dict):
        raise ValueError("invalid_preferences")
    action = raw.get("next_action")
    if action is not None and not isinstance(action, str):
        raise ValueError("invalid_next_action")
    uncertainty = raw.get("uncertainty")
    if not isinstance(uncertainty, str):
        raise ValueError("uncertainty_required")
    return {
        "commercial_status": status,
        "contact_effective": raw.get("contact_effective"),
        "follow_up_required": raw.get("follow_up_required"),
        "opt_out": raw.get("opt_out"),
        "next_action": action,
        "preferences": preferences,
        "evidence_provider_keys": evidence,
        "uncertainty": uncertainty,
        "coverage_complete": bool(prepared["coverage_complete"]),
        "review_status": "PENDING_HUMAN_REVIEW",
        "campaign_eligible": False,
    }
