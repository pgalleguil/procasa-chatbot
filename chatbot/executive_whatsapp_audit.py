"""Preparación aislada de auditoría WhatsApp de ejecutivos (NO habilitada en producción).

No envía WhatsApp, no crea leads, no altera chatbot/SLA/gestiones.
La integración HTTP debe autenticar cada sesión y llamar a ingest_upsert desde
una ruta DISTINTA a la del chatbot de oficina, tras revisión de seguridad.
"""
from __future__ import annotations

import hmac
from datetime import datetime, timezone
from typing import Any, Mapping

from .phone_utils import has_real_phone, normalize_phone_strict, phone_identity_variants

EVENT_NAME = "messages.upsert"
MAX_MESSAGE_CHARACTERS = 20_000


class ExecutiveAuditError(ValueError):
    """Evento rechazado por seguridad o identidad insuficiente."""


def verify_webhook_secret(provided: str | None, expected: str | None) -> None:
    """WaSender documenta comparación directa de X-Webhook-Signature con el secreto.

    NO asumir HMAC: la guía actual compara el encabezado con el secreto.
    El secreto corresponde a una sesión autorizada, nunca a un valor del payload.
    """
    if not provided or not expected or not hmac.compare_digest(
        str(provided), str(expected)
    ):
        raise ExecutiveAuditError("invalid_webhook_signature")


def _datetime_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
            if number > 10_000_000_000:
                number /= 1000
            dt = datetime.fromtimestamp(number, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    elif isinstance(value, str) and value.strip():
        val = value.strip()
        if val.isdigit():
            return _datetime_utc(int(val))
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _canonical_peer(key: Mapping[str, Any], outgoing: bool) -> str | None:
    jid = str(key.get("remoteJid") or "").strip()
    # WhatsApp @lid is an opaque identifier, NOT a phone number.
    if jid.endswith(("@s.whatsapp.net", "@c.us")):
        raw = jid.split("@", 1)[0]
    elif "@" not in jid and jid:
        raw = jid
    elif jid.endswith("@lid") and not outgoing:
        # For an incoming LID only, WaSender may provide the real sender PN.
        raw = str(key.get("senderPn") or key.get("cleanedSenderPn") or "")
        if "@" in raw:
            raw = raw.split("@", 1)[0]
    else:
        return None
    normalized = normalize_phone_strict(raw)
    return normalized if normalized and has_real_phone(normalized) else None


def _extract_body(item: Mapping[str, Any]) -> tuple[str | None, str]:
    body = item.get("messageBody")
    detail = item.get("message")
    if not isinstance(detail, dict):
        detail = {}
    if not isinstance(body, str):
        body = detail.get("conversation")
    if not isinstance(body, str):
        extended = detail.get("extendedTextMessage")
        body = extended.get("text") if isinstance(extended, dict) else None
    if isinstance(body, str) and body.strip():
        if len(body) > MAX_MESSAGE_CHARACTERS:
            return None, "oversized_text"
        return body, "text"
    # Media without a supported text transcription is NOT invented as text.
    for name, kind in (
        ("imageMessage", "image"), ("videoMessage", "video"),
        ("audioMessage", "audio"), ("documentMessage", "document"),
    ):
        if name in detail:
            media = detail.get(name)
            caption = media.get("caption") if isinstance(media, dict) else None
            if isinstance(caption, str) and len(caption) <= MAX_MESSAGE_CHARACTERS:
                return caption, kind
            return None, kind
    return None, "unsupported"


def normalize_upsert(
    payload: Mapping[str, Any], *, session_id: str, executive_user_id: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Extrae mensajes 1:1, nunca grupos, status, campañas ni chats personales sin match.

    La sesión y el ejecutivo provienen de la configuración confiable del servidor,
    jamás del cuerpo del webhook. Compatible con data.messages object o array.
    """
    if not session_id or not executive_user_id:
        raise ExecutiveAuditError("unregistered_executive_session")
    if not isinstance(payload, Mapping) or payload.get("event") != EVENT_NAME:
        return [], ["ignored_event"]
    data = payload.get("data")
    if not isinstance(data, dict):
        return [], ["invalid_data"]
    source = data.get("messages")
    items = source if isinstance(source, list) else [source]
    if not items or items == [None]:
        return [], ["empty_message"]
    records: list[dict[str, Any]] = []
    skipped: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            skipped.append("invalid_message")
            continue
        key = item.get("key")
        if not isinstance(key, dict) or not str(key.get("id") or "").strip():
            skipped.append("missing_provider_message_id")
            continue
        if not isinstance(key.get("fromMe"), bool):
            skipped.append("missing_message_direction")
            continue
        outgoing = key["fromMe"]
        phone = _canonical_peer(key, outgoing)
        if not phone:
            skipped.append("unresolved_or_nonprivate_peer")
            continue
        content, kind = _extract_body(item)
        if kind in {"unsupported", "oversized_text"}:
            skipped.append(kind)
            continue
        msg_dt = _datetime_utc(
            item.get("messageTimestamp")
            or item.get("timestamp")
            or (item.get("message") or {}).get("messageTimestamp")
        )
        ts_origin = "message_timestamp" if msg_dt else "webhook_timestamp"
        msg_dt = msg_dt or _datetime_utc(payload.get("timestamp"))
        if not msg_dt:
            skipped.append("missing_timestamp")
            continue
        key_id = str(key["id"]).strip()
        records.append({
            "provider_key": f"{session_id}:{EVENT_NAME}:{phone}:{key_id}",
            "provider_message_id": key_id,
            "actor_type": "executive" if outgoing else "client",
            "executive_user_id": str(executive_user_id),
            "session_id": str(session_id),
            "phone": phone,
            "content": content,
            "message_type": kind,
            "timestamp": msg_dt,
            "timestamp_origin": ts_origin,
            "ingestion_source": "wasender_webhook",
            "channel": "executive_whatsapp",
        })
    return records, skipped


def _find_cycle_for_message(db: Any, phone: str, executive_user_id: str,
                            at: datetime) -> tuple[Any | None, dict | None, str]:
    """Match conservador por teléfono + ciclo de asignación en su ventana temporal."""
    variants = phone_identity_variants(phone)
    if not variants:
        return None, None, "invalid_peer"
    or_filter = [
        {"phone": {"$in": variants}},
        {"contact_phone": {"$in": variants}},
        {"contact_phone_normalized": {"$in": variants}},
        {"prospecto.phone": {"$in": variants}},
    ]
    leads = list(db["leads"].find({"$or": or_filter}, {"_id": 1}))
    if not leads:
        return None, None, "lead_not_found"
    ids = [lead["_id"] for lead in leads]
    cycles = list(db["crm_assignment_cycles"].find(
        {"lead_id": {"$in": ids}},
        {"lead_id": 1, "assignment_cycle_id": 1, "assigned_to_user_id": 1,
         "assigned_at": 1, "cycle_started_at": 1, "unassigned_at": 1,
         "closed_at": 1, "cycle_status": 1},
    ))
    eligible = []
    for lead in leads:
        per_lead = [c for c in cycles if c.get("lead_id") == lead["_id"]]
        starts = []
        for cycle in per_lead:
            begin = _datetime_utc(cycle.get("assigned_at") or cycle.get("cycle_started_at"))
            if begin is not None:
                starts.append((begin, cycle))
        for begin, cycle in starts:
            if str(cycle.get("assigned_to_user_id") or "") != str(executive_user_id):
                continue
            if begin > at or not cycle.get("assignment_cycle_id"):
                continue
            later = [t for t, other in starts if
                     other.get("assignment_cycle_id") != cycle.get("assignment_cycle_id")
                     and t > begin]
            explicit_end = _datetime_utc(cycle.get("unassigned_at") or cycle.get("closed_at"))
            cutoffs = later + ([explicit_end] if explicit_end is not None else [])
            if cutoffs and at >= min(cutoffs):
                continue
            # A closed/reassigned cycle without a reliable end is not attributable.
            if not cutoffs and str(cycle.get("cycle_status") or "").lower() in (
                "closed", "reassigned"
            ):
                continue
            eligible.append((lead, cycle))
    if not eligible:
        return None, None, "no_confirmed_assignment_at_message_time"
    if len(eligible) != 1:
        return None, None, "ambiguous_lead_or_assignment"
    return eligible[0][0], eligible[0][1], "matched"


def ingest_upsert(
    db: Any, payload: Mapping[str, Any], *,
    session_id: str, executive_user_id: str,
    signature_header: str | None, webhook_secret: str | None,
    commit: bool = False,
) -> dict[str, Any]:
    """Dry-run por defecto. No toca mensajes existentes ni altera el ciclo/SLA.

    El caller debe obtener session_id desde su ruta autenticada y configurar el
    par (session_id -> executive_user_id, webhook_secret) fuera del payload.
    No invocar desde el handler del chatbot de oficina.
    """
    verify_webhook_secret(signature_header, webhook_secret)
    records, skipped = normalize_upsert(
        payload, session_id=session_id, executive_user_id=executive_user_id
    )
    statuses: dict[str, int] = {}
    for reason in skipped:
        statuses[reason] = statuses.get(reason, 0) + 1
    seen: set[str] = set()
    for record in records:
        if record["provider_key"] in seen:
            statuses["duplicate_in_batch"] = statuses.get("duplicate_in_batch", 0) + 1
            continue
        seen.add(record["provider_key"])
        lead, cycle, reason = _find_cycle_for_message(
            db, record["phone"], executive_user_id, record["timestamp"]
        )
        if reason != "matched":
            statuses[reason] = statuses.get(reason, 0) + 1
            continue
        if not commit:
            statuses["planned"] = statuses.get("planned", 0) + 1
            continue
        message = {k: v for k, v in record.items() if k != "phone"}
        message["assignment_cycle_id"] = str(cycle["assignment_cycle_id"])
        # Atomic condition suppresses duplicates across concurrent workers.
        result = db["leads"].update_one(
            {"_id": lead["_id"],
             "executive_whatsapp_messages.provider_key": {"$ne": record["provider_key"]}},
            {"$push": {"executive_whatsapp_messages": message}},
            upsert=False,
        )
        status = "saved" if result.modified_count == 1 else "duplicate_or_missing"
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "mode": "persist" if commit else "dry_run",
        "session_id": session_id,
        "accepted_events": len(records),
        "results": statuses,
        "chatbot_untouched": True,
        "outbound_messages_sent": 0,
    }


AUDIT_JSON_FIELDS = (
    "commercial_status", "contact_effective", "follow_up_required",
    "next_action", "preferences", "opt_out", "evidence_provider_keys",
    "uncertainty", "coverage_complete"
)


def audit_transcript(lead: Mapping[str, Any], *, limit: int = 300) -> list[dict]:
    """Vista unificada, sin modificar el array messages que utiliza el chatbot."""
    timeline = []
    for row in lead.get("messages", []):
        if not isinstance(row, dict):
            continue
        ts = _datetime_utc(row.get("timestamp"))
        role = str(row.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        if ts:
            timeline.append({
                "timestamp": ts.isoformat(),
                "actor_type": "client" if role == "user" else "chatbot",
                "content": str(row.get("content") or ""),
                "source": "office_chatbot",
                "evidence_key": str(row.get("source_event_id") or ""),
            })
    for row in lead.get("executive_whatsapp_messages", []):
        if not isinstance(row, dict):
            continue
        ts = _datetime_utc(row.get("timestamp"))
        if ts:
            timeline.append({
                "timestamp": ts.isoformat(),
                "actor_type": row.get("actor_type"),
                "executive_user_id": row.get("executive_user_id"),
                "assignment_cycle_id": row.get("assignment_cycle_id"),
                "content": row.get("content"),
                "message_type": row.get("message_type"),
                "source": "executive_whatsapp",
                "evidence_key": row.get("provider_key"),
            })
    timeline.sort(key=lambda item: (item["timestamp"], item["source"]))
    return timeline[-limit:]
