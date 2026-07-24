"""WhatsApp message templates for PROCASA CRM notifications.

All user-facing strings live here so that wording, hot reasons, and SLA
thresholds can be reviewed and changed without touching business logic.
The module is deliberately provider-agnostic — it returns plain text str.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional
from urllib.parse import quote, urlencode

from config import Config


# ---------------------------------------------------------------------------
# Hot reason display mapping
# ---------------------------------------------------------------------------

HOT_REASON_MAP: dict[str, str] = {
    "ASK_VISIT": "Quiere coordinar una visita",
    "ASK_CONTACT": "Solicit\u00f3 hablar con un ejecutivo",
    "GIVE_OFFER": "Manifest\u00f3 intenci\u00f3n de avanzar o realizar una oferta",
    "ESCALATED_URGENT": "Requiere atenci\u00f3n prioritaria",
    "ESCALADO_URGENTE": "Requiere atenci\u00f3n prioritaria",
    "VISIT_CONFIRMATION": "Confirm\u00f3 que desea visitar la propiedad",
    "INTERES_VISITA": "Quiere coordinar una visita",
    "SOLICITUD_CONTACTO": "Solicit\u00f3 hablar con un ejecutivo",
    "LEAD_HOT_WHATSAPP": "Present\u00f3 una nueva señal comercial prioritaria",
    "LeadHotWhatsapp": "Present\u00f3 una nueva señal comercial prioritaria",
}

FALLBACK_HOT_REASON = "Present\u00f3 una nueva señal comercial prioritaria"


def display_hot_reason(raw: Any) -> str:
    """Convert an internal hot reason/enum to a human-readable string."""
    if not raw:
        return FALLBACK_HOT_REASON
    key = str(raw).strip().upper()
    return HOT_REASON_MAP.get(key, FALLBACK_HOT_REASON)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _val(data: Mapping[str, Any], *keys: str, default: str = "") -> str:
    """Safely extract a nested value from a dict."""
    for key in keys:
        parts = key.split(".")
        val: Any = data
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                return default
        if val is not None and str(val).strip() and str(val).strip().lower() != "none":
            return str(val).strip()
    return default


def _lead_url(lead_data: Mapping[str, Any], property_code: Optional[str] = None) -> str:
    """Build a deep link to the CRM lead detail page."""
    from .phone_utils import is_synthetic_phone

    base = str(getattr(Config, "CRM_BASE_URL", "https://procasa-chatbot-yr8d.onrender.com")).rstrip("/")
    phone = str(_val(lead_data, "lead_phone", "phone") or "")
    if not phone:
        return f"{base}/crm?temperatura=HOT"
    if is_synthetic_phone(phone):
        url = f"{base}/crm/lead/{quote(phone, safe='')}"
    else:
        url = f"{base}/crm/lead/{quote(phone.replace('+', '').strip(), safe='')}"
    code = property_code or _val(lead_data, "property_code", "codigo", "prospecto.codigo")
    if code and code not in ("", "N/D", "S/N", "NONE"):
        url += "?" + urlencode({"codigo": code})
    return url


def _crm_filtered_url(executive_name: Optional[str] = None, extra_params: Optional[dict] = None) -> str:
    """Build a CRM list URL filtered for the executive's pending leads."""
    base = str(getattr(Config, "CRM_BASE_URL", "https://procasa-chatbot-yr8d.onrender.com")).rstrip("/")
    params: dict[str, str] = {"temperatura": "COLD", "orden": "antiguos_sin_atender"}
    if executive_name:
        params["ejecutivo"] = executive_name
    if extra_params:
        params.update(extra_params)
    return f"{base}/crm?" + urlencode(params)


def _preview_lines(leads: list[Mapping[str, Any]], max_items: int) -> list[str]:
    """Build up to max_items preview strings from lead data."""
    lines = []
    for i, ld in enumerate(leads[:max_items]):
        name = _val(ld, "nombre", "prospecto.nombre") or "Cliente"
        prop = _val(ld, "property_code", "codigo", "prospecto.codigo") or "S/N"
        comuna = _val(ld, "comuna", "prospecto.comuna")
        loc = f" — {comuna}" if comuna else ""
        lines.append(f"{i+1}. {name} — {prop}{loc}")
    return lines


def _time_str(value: Any) -> str:
    """Format a datetime-like value to a short readable time."""
    if not value:
        return ""
    import pytz
    from datetime import datetime
    CL_TZ = pytz.timezone("America/Santiago")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return str(value)
    if hasattr(value, "astimezone"):
        try:
            value = value.astimezone(CL_TZ)
        except (OSError, ValueError):
            pass
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    return str(value)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

# 3. SINGLE NON-HOT LEAD
def single_non_hot(
    executive_name: str,
    client_name: str,
    property_summary: str,
    commune: str,
    source: str,
    received_time: str,
) -> str:
    return (
        f"\U0001f4e5 *NUEVO LEAD POR CALIFICAR*\n\n"
        f"Hola, {executive_name}. Tienes un nuevo lead asignado:\n\n"
        f"\U0001f464 *Cliente:* {client_name}\n"
        f"\U0001f3e0 *Propiedad:* {property_summary}\n"
        f"\U0001f4cd *Comuna:* {commune}\n"
        f"\U0001f310 *Origen:* {source}\n"
        f"\U0001f550 *Recibido:* {received_time}\n\n"
        f"Este lead todav\u00eda no presenta una se\u00f1al comercial prioritaria.\n\n"
        f"Revisa el contacto y registra el resultado de tu gesti\u00f3n en el CRM.\n"
    )


# 4. MULTIPLE NON-HOT LEADS
def multiple_non_hot(
    executive_name: str,
    lead_count: int,
    lead_previews: list[str],
    remaining_count: int = 0,
) -> str:
    lines = [
        f"\U0001f4e5 *NUEVOS LEADS POR CALIFICAR*\n",
        f"Hola, {executive_name}. Tienes *{lead_count} nuevos leads asignados* "
        f"durante los \u00faltimos minutos:\n",
    ]
    lines.extend(lead_previews)
    lines.append("")
    if remaining_count > 0:
        lines.append(f"\u2026y {remaining_count} {'lead' if remaining_count == 1 else 'leads'} adicionales.\n")
    lines.append(
        "Estos leads todav\u00eda no presentan se\u00f1ales comerciales prioritarias.\n\n"
        "Revisa cada contacto y registra el resultado de las gestiones en el CRM.\n"
    )
    return "\n".join(lines)


# 5. LEAD ENTERS DIRECTLY AS HOT
def hot_direct(
    executive_name: str,
    client_name: str,
    property_summary: str,
    commune: str,
    hot_reason_display: str,
    hot_since_time: str,
) -> str:
    return (
        f"\U0001f525 *LEAD HOT \u2014 ATENCI\u00d3N PRIORITARIA*\n\n"
        f"Hola, {executive_name}. Se te ha asignado un Lead Hot que requiere atenci\u00f3n prioritaria.\n\n"
        f"\U0001f464 *Cliente:* {client_name}\n"
        f"\U0001f3e0 *Propiedad:* {property_summary}\n"
        f"\U0001f4cd *Comuna:* {commune}\n"
        f"\U0001f525 *Motivo:* {hot_reason_display}\n"
        f"\U0001f550 *Detectado como Hot:* {hot_since_time}\n\n"
        f"Contacta al cliente lo antes posible y registra el resultado de tu gesti\u00f3n en el CRM.\n"
    )


# 6. BECOMES HOT BEFORE DIGEST
def hot_before_digest(
    executive_name: str,
    client_name: str,
    property_summary: str,
    hot_reason_display: str,
    hot_since_time: str,
) -> str:
    return (
        f"\U0001f525 *LEAD HOT \u2014 ATENCI\u00d3N PRIORITARIA*\n\n"
        f"Hola, {executive_name}. Un lead reci\u00e9n asignado present\u00f3 una "
        f"se\u00f1al comercial prioritaria.\n\n"
        f"\U0001f464 *Cliente:* {client_name}\n"
        f"\U0001f3e0 *Propiedad:* {property_summary}\n"
        f"\U0001f525 *Motivo:* {hot_reason_display}\n"
        f"\U0001f550 *Pas\u00f3 a Hot:* {hot_since_time}\n\n"
        f"Contacta al cliente lo antes posible y registra el resultado de tu gesti\u00f3n en el CRM.\n"
    )


# 7. BECOMES HOT AFTER DIGEST
def hot_after_digest(
    executive_name: str,
    client_name: str,
    property_summary: str,
    hot_reason_display: str,
    hot_since_time: str,
) -> str:
    return (
        f"\U0001f525 *LEAD ASIGNADO PAS\u00d3 A HOT*\n\n"
        f"Hola, {executive_name}. Un lead que ya estaba asignado a ti acaba de "
        f"presentar una se\u00f1al comercial prioritaria.\n\n"
        f"\U0001f464 *Cliente:* {client_name}\n"
        f"\U0001f3e0 *Propiedad:* {property_summary}\n"
        f"\U0001f525 *Nuevo motivo:* {hot_reason_display}\n"
        f"\U0001f550 *Pas\u00f3 a Hot:* {hot_since_time}\n\n"
        f"No es una nueva asignaci\u00f3n. Es una actualizaci\u00f3n prioritaria del mismo lead.\n\n"
        f"Contacta al cliente y registra el resultado de la gesti\u00f3n en el CRM.\n"
    )


# 8. MANAGED LEAD THAT BECOMES HOT
def hot_after_management(
    executive_name: str,
    client_name: str,
    property_summary: str,
    hot_reason_display: str,
    hot_since_time: str,
) -> str:
    return (
        f"\U0001f525 *LEAD GESTIONADO PAS\u00d3 A HOT \u2014 REQUIERE SEGUIMIENTO*\n\n"
        f"Hola, {executive_name}. Un lead que ya hab\u00edas gestionado present\u00f3 "
        f"una nueva se\u00f1al comercial prioritaria.\n\n"
        f"\U0001f464 *Cliente:* {client_name}\n"
        f"\U0001f3e0 *Propiedad:* {property_summary}\n"
        f"\U0001f525 *Nuevo motivo:* {hot_reason_display}\n"
        f"\U0001f550 *Escalamiento:* {hot_since_time}\n\n"
        f"La primera gesti\u00f3n ya se encuentra registrada. Este aviso requiere un "
        f"nuevo seguimiento prioritario.\n\n"
        f"Realiza el seguimiento y registra la nueva actividad en el CRM.\n"
    )


# ---------------------------------------------------------------------------
# SLA alert templates
# ---------------------------------------------------------------------------

# 11. NON-HOT SLA 150 min precritical (grouped)
def sla_non_hot_precritical_150(
    executive_name: str,
    lead_count: int,
    lead_previews: list[str],
) -> str:
    lines = [
        f"\u26a0\ufe0f *LEADS PR\u00d3XIMOS A SLA CR\u00cdTICO*\n",
        f"Hola, {executive_name}. Tienes *{lead_count} {'lead' if lead_count == 1 else 'leads'} por calificar* "
        f"pr\u00f3ximos a alcanzar el l\u00edmite de gesti\u00f3n.\n\n"
        f"Quedan aproximadamente 30 minutos h\u00e1biles antes de que se consideren cr\u00edticos.\n",
    ]
    lines.extend(lead_previews)
    lines.extend([
        "",
        "Gestiona los contactos pendientes y registra los resultados en el CRM.\n",
    ])
    return "\n".join(lines)


# 12. NON-HOT SLA 180 min critical
def sla_non_hot_critical_180(
    executive_name: str,
    lead_count: int,
    lead_previews: list[str],
) -> str:
    lines = [
        f"\U0001f534 *LEADS CON SLA CR\u00cdTICO*\n",
        f"Hola, {executive_name}. Tienes *{lead_count} {'lead' if lead_count == 1 else 'leads'} por calificar* "
        f"que alcanzaron el l\u00edmite de gesti\u00f3n sin una actividad v\u00e1lida registrada.\n",
    ]
    lines.extend(lead_previews)
    lines.extend([
        "",
        "Gestiona estos contactos y registra inmediatamente los resultados en el CRM.\n",
    ])
    return "\n".join(lines)


def sla_non_hot_critical_180_supervisor(
    executive_name: str,
    lead_count: int,
    supervisor_crm_url: str,
) -> str:
    return (
        f"\U0001f534 *ALERTA DE SLA \u2014 LEADS SIN GESTI\u00d3N*\n\n"
        f"{executive_name} tiene {lead_count} {'lead' if lead_count == 1 else 'leads'} por calificar "
        f"que alcanzaron el SLA cr\u00edtico.\n\n"
        f"No se realiz\u00f3 ninguna reasignaci\u00f3n autom\u00e1tica.\n\n"
        f"\U0001f449 *Revisar casos:* {supervisor_crm_url}\n"
    )


# 14. HOT SLA 45 min precritical
def sla_hot_precritical_45(
    executive_name: str,
    client_name: str,
    property_summary: str,
    hot_reason_display: str,
) -> str:
    return (
        f"\U0001f6a8 *LEAD HOT PENDIENTE \u2014 15 MINUTOS PARA SLA CR\u00cdTICO*\n\n"
        f"Hola, {executive_name}. Este Lead Hot contin\u00faa sin una gesti\u00f3n "
        f"registrada y est\u00e1 pr\u00f3ximo a alcanzar el l\u00edmite cr\u00edtico.\n\n"
        f"\U0001f464 *Cliente:* {client_name}\n"
        f"\U0001f3e0 *Propiedad:* {property_summary}\n"
        f"\U0001f525 *Motivo:* {hot_reason_display}\n"
        f"\u23f1\ufe0f *Tiempo transcurrido:* 45 minutos h\u00e1biles\n"
        f"\u23f3 *Tiempo restante:* aproximadamente 15 minutos h\u00e1biles\n\n"
        f"Gestiona el contacto inmediatamente y registra el resultado en el CRM.\n"
    )


# 15. HOT SLA 60 min critical
def sla_hot_critical_60(
    executive_name: str,
    client_name: str,
    property_summary: str,
    hot_reason_display: str,
    elapsed_minutes: int,
) -> str:
    return (
        f"\U0001f534 *LEAD HOT CON SLA CR\u00cdTICO*\n\n"
        f"Hola, {executive_name}. Este Lead Hot alcanz\u00f3 el l\u00edmite cr\u00edtico "
        f"sin una gesti\u00f3n v\u00e1lida registrada.\n\n"
        f"\U0001f464 *Cliente:* {client_name}\n"
        f"\U0001f3e0 *Propiedad:* {property_summary}\n"
        f"\U0001f525 *Motivo:* {hot_reason_display}\n"
        f"\u23f1\ufe0f *Tiempo sin gesti\u00f3n:* {elapsed_minutes} minutos h\u00e1biles\n\n"
        f"Gestiona el contacto inmediatamente y registra el resultado en el CRM.\n"
    )


def sla_hot_critical_60_supervisor(
    executive_name: str,
    client_name: str,
    property_summary: str,
    hot_reason_display: str,
    elapsed_minutes: int,
) -> str:
    return (
        f"\U0001f534 *ALERTA CR\u00cdTICA \u2014 LEAD HOT SIN GESTI\u00d3N*\n\n"
        f"El siguiente Lead Hot alcanz\u00f3 el SLA cr\u00edtico bajo responsabilidad de "
        f"{executive_name}:\n\n"
        f"\U0001f464 *Cliente:* {client_name}\n"
        f"\U0001f3e0 *Propiedad:* {property_summary}\n"
        f"\U0001f525 *Motivo:* {hot_reason_display}\n"
        f"\u23f1\ufe0f *Tiempo:* {elapsed_minutes} minutos h\u00e1biles\n\n"
        f"No se realiz\u00f3 ninguna reasignaci\u00f3n autom\u00e1tica.\n\n"
        f"\U0001f449 *Revisar lead:* {_lead_url({'lead_phone': '', 'property_code': property_summary})}\n"
    )
