"""WhatsApp message templates for CRM SLA alerts.

Headers reflect action detection: "resultado pendiente" when outreach exists.
"""
from __future__ import annotations

from config import Config

MESSAGE_DOMAIN = "crm_sla_alert"

# ---------------------------------------------------------------------------
# Explanations per outreach state
# ---------------------------------------------------------------------------

EXPLAIN = {
    "none": (
        "Todav\u00eda no existe una acci\u00f3n de contacto "
        "ni un resultado de gesti\u00f3n registrado para este lead."
    ),
    "whatsapp_opened": (
        "El CRM detect\u00f3 que abriste WhatsApp, pero a\u00fan "
        "no registraste el resultado del contacto."
    ),
    "whatsapp_sent": (
        "El CRM registr\u00f3 que enviaste un WhatsApp, pero "
        "a\u00fan no registraste el resultado del contacto."
    ),
    "phone_opened": (
        "El CRM detect\u00f3 que abriste el tel\u00e9fono del "
        "cliente, pero a\u00fan no registraste el resultado."
    ),
    "call_without_result": (
        "El CRM registr\u00f3 una llamada, pero todav\u00eda no "
        "existe un resultado de gesti\u00f3n registrado."
    ),
    "email_opened": (
        "El CRM detect\u00f3 que abriste el correo del "
        "cliente, pero a\u00fan no registraste el resultado."
    ),
    "email_sent": (
        "El CRM registr\u00f3 que enviaste un correo, pero "
        "a\u00fan no registraste el resultado del contacto."
    ),
}

ACTION = {
    "none": "Contacta al cliente y registra el resultado en el CRM.",
    "whatsapp_opened": (
        "Abrir WhatsApp no detiene el SLA. "
        "Registra ahora si hubo contacto, si no respondi\u00f3 "
        "o cu\u00e1l ser\u00e1 el pr\u00f3ximo paso."
    ),
    "whatsapp_sent": (
        "Enviar un WhatsApp no detiene el SLA. "
        "Registra ahora si hubo contacto, si no respondi\u00f3 "
        "o cu\u00e1l ser\u00e1 el pr\u00f3ximo paso."
    ),
    "phone_opened": (
        "Abrir el tel\u00e9fono del cliente no detiene el SLA. "
        "Registra ahora si hubo contacto, si no respondi\u00f3 "
        "o cu\u00e1l ser\u00e1 el pr\u00f3ximo paso."
    ),
    "call_without_result": (
        "Realizar una llamada sin registrar su resultado "
        "no detiene el SLA. "
        "Registra ahora el resultado o el pr\u00f3ximo paso."
    ),
    "email_opened": (
        "Abrir un correo no detiene el SLA. "
        "Registra ahora si hubo contacto, si no respondi\u00f3 "
        "o cu\u00e1l ser\u00e1 el pr\u00f3ximo paso."
    ),
    "email_sent": (
        "Enviar un correo no detiene el SLA. "
        "Registra ahora si hubo contacto, si no respondi\u00f3 "
        "o cu\u00e1l ser\u00e1 el pr\u00f3ximo paso."
    ),
}

BREACHED_ACTION_NONE = (
    "Contacta al cliente y registra inmediatamente "
    "el resultado en el CRM."
)

CHANNEL_LABELS: dict[str, str] = {
    "whatsapp_opened": "abriste WhatsApp",
    "whatsapp_sent": "enviaste un WhatsApp",
    "phone_opened": "abriste el tel\u00e9fono del cliente",
    "call_without_result": "realizaste una llamada sin resultado registrado",
    "email_opened": "abriste el correo del cliente",
    "email_sent": "enviaste un correo",
}


def outreach_channel_label(outreach_state: str) -> str:
    return CHANNEL_LABELS.get(outreach_state, "")


def build_sla_message(
    *,
    hot: bool,
    breached: bool,
    client_first_name: str,
    property_code: str,
    elapsed_minutes: int,
    deadline_display: str,
    lead_url: str,
    outreach_state: str,
) -> str:
    limit = 60 if hot else 180
    has_action = outreach_state != "none"

    # Headers with action detection
    if hot and breached:
        title = (
            "\U0001f525\U0001f6a8 *Lead Hot vencido: resultado pendiente*"
            if has_action else
            "\U0001f525\U0001f6a8 *Lead Hot con SLA vencido*"
        )
        timing = (
            f"SLA Hot utilizado: {elapsed_minutes} minutos h\u00e1biles\n"
            f"Venci\u00f3 el: {deadline_display}"
        )
        link_label = "\U0001f4dd *Registrar resultado:*"
    elif hot and not breached:
        title = (
            "\U0001f525\u26a0\ufe0f *Lead Hot pr\u00f3ximo a vencer: resultado pendiente*"
            if has_action else
            "\U0001f525\u26a0\ufe0f *Lead Hot pr\u00f3ximo a vencer*"
        )
        timing = (
            f"SLA Hot utilizado: {elapsed_minutes} de {limit} minutos h\u00e1biles\n"
            f"Hora l\u00edmite: {deadline_display}"
        )
        link_label = "\U0001f4dd *Registrar resultado:*"
    elif breached:
        title = (
            "\U0001f6a8 *Resultado pendiente: SLA vencido*"
            if has_action else
            "\U0001f6a8 *Lead con SLA vencido*"
        )
        timing = (
            f"SLA utilizado: {elapsed_minutes} minutos h\u00e1biles\n"
            f"Venci\u00f3 el: {deadline_display}"
        )
        link_label = "\U0001f517 *Registrar gesti\u00f3n:*"
    else:
        title = (
            "\u26a0\ufe0f *Resultado pendiente: lead pr\u00f3ximo a vencer*"
            if has_action else
            "\u26a0\ufe0f *Lead pr\u00f3ximo a vencer*"
        )
        timing = (
            f"SLA utilizado: {elapsed_minutes} de {limit} minutos h\u00e1biles\n"
            f"Hora l\u00edmite: {deadline_display}"
        )
        link_label = "\U0001f517 *Registrar gesti\u00f3n:*"

    explain = EXPLAIN.get(outreach_state, EXPLAIN["none"])
    action = ACTION.get(outreach_state, ACTION["none"])

    if breached and outreach_state == "none":
        action = BREACHED_ACTION_NONE

    return (
        f"{title}\n\n"
        f"Cliente: {client_first_name}\n"
        f"Propiedad: {property_code}\n"
        f"{timing}\n\n"
        f"{explain}\n\n"
        f"{action}\n\n"
        f"{link_label} {lead_url}"
    )


def build_lead_url(lead: dict) -> str:
    base = str(
        getattr(Config, "CRM_PUBLIC_BASE_URL", None)
        or getattr(Config, "CRM_BASE_URL", "")
    ).rstrip("/")
    lid = lead.get("_id", "")
    return f"{base}/crm/lead-id/{lid}"


def build_deadline_display(deadline_dt, tz) -> str:
    if deadline_dt is None:
        return "--/--/---- --:--"
    if deadline_dt.tzinfo is None:
        from datetime import timezone as _utc
        deadline_dt = deadline_dt.replace(tzinfo=_utc.utc)
    local = deadline_dt.astimezone(tz)
    return local.strftime("%d/%m/%Y %H:%M")
