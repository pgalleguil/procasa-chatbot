"""Administrative handling of unresolved real-estate publication references.

These notices are not executive assignments. Keep their recipient restricted
to the configured PROCASA administrator and retain the source link for review.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

MISSING_PROPERTY_ADMIN_PHONE = "56983219804"


def _phone_digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def is_missing_property_admin_alert(notification: Mapping[str, Any], target_phone: Any = None) -> bool:
    payload = notification.get("lead_data") or notification
    recipient = target_phone or payload.get("target_phone") or notification.get("target_phone")
    return (
        payload.get("lead_type") == "MissingProperty"
        and payload.get("assignment_type") == "MISSING_PROPERTY"
        and _phone_digits(recipient) == MISSING_PROPERTY_ADMIN_PHONE
        and _phone_digits(payload.get("target_phone")) == MISSING_PROPERTY_ADMIN_PHONE
    )


def _summary_fields(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    raw_message = str(payload.get("last_message") or "")
    match = re.search(r"https?://[^\s<>\]\)\"]+", raw_message, re.I)
    link = match.group(0).rstrip(".,;:!?") if match else ""
    code_match = re.search(r"\bMLC[-_]?(\d+)\b", raw_message, re.I)
    external_code = f"MLC-{code_match.group(1)}" if code_match else "Sin identificar"
    return str(payload.get("phone") or "No disponible"), external_code, link


def format_missing_property_admin_alert(payload: Mapping[str, Any]) -> str:
    phone, code, link = _summary_fields(payload)
    lines = [
        "*PROCASA - Propiedad no encontrada*",
        f"Lead: {phone}",
        f"Publicación: {code}",
    ]
    if link:
        lines.append(f"Enlace enviado: {link}")
    else:
        lines.append(f"Consulta: {str(payload.get('last_message') or '')[:300]}")
    lines.append("Revisar referencia en Prop360. No se asignó ejecutivo ni se inició SLA.")
    return "\n".join(lines)


def format_missing_property_admin_summary(notifications: list[Mapping[str, Any]]) -> str:
    lines = ["*PROCASA - Publicaciones pendientes de revisión*"]
    for notification in notifications:
        payload = notification.get("lead_data") or notification
        phone, code, link = _summary_fields(payload)
        lines.append(f"Lead: {phone} | {code}")
        if link:
            lines.append(link)
    lines.append("Revisar en Prop360; no asignar sin verificar la propiedad.")
    return "\n".join(lines)
