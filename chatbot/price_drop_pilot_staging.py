"""Read-only preflight for the PROCASA price-drop pilot.

There is deliberately no sender, write operation, approval or release switch.
Candidate records are for internal review ONLY. Do not use these previews as
proof of marketing consent or permission to initiate a WhatsApp conversation.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any

from .phone_utils import has_real_phone, normalize_phone_strict, phone_identity_variants
from .price_drop_pilot import COLLECTION, _money

CODE = "6436"
MAX_REVIEW = 3
MAX_PROPERTY_LEADS = 300
STOP = re.compile(
    r"\b(?:no me (?:escriban|contacten|envien)|no quiero (?:mas )?mensajes|"
    r"darme de baja|eliminar mi numero|stop|baja de whatsapp|no me interesa)\b",
    re.IGNORECASE,
)
NAME = re.compile(
    r"\b(?:me llamo|mi nombre es)\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,25})\b",
    re.IGNORECASE,
)


def _date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _uf(value: Any) -> str:
    return f"{float(value):,.0f}".replace(",", ".")


def _verified_drop(db: Any, code: str) -> dict | None:
    """Require a real Prop360 history event, current listing and exact price."""
    histories = list(db["universo_cartera_prop360_historial"].find(
        {"codigo": code, "campo": "precio_publicado", "tipo_evento": "cambio_precio",
         "moneda_anterior": "UF", "moneda_nueva": "UF"},
        {"codigo": 1, "fecha": 1, "monto_anterior": 1, "monto_nuevo": 1},
    ).sort("fecha", -1).limit(20))
    prop = db["universo_cartera_prop360"].find_one(
        {"codigo": code},
        {"codigo": 1, "disponible_prop360": 1, "estado.oficina": 1,
         "tipo_operacion.venta": 1, "tipo_operacion.precio_venta.precio_uf": 1,
         "ubicacion.comuna": 1},
    ) or {}
    operation = prop.get("tipo_operacion") or {}
    current = _money((operation.get("precio_venta") or {}).get("precio_uf"))
    commune = str((prop.get("ubicacion") or {}).get("comuna") or "").strip()
    if (str(prop.get("codigo")) != code or not prop.get("disponible_prop360")
        or (prop.get("estado") or {}).get("oficina") != "PROCASA SUCRE"
        or operation.get("venta") is not True or current is None
        or not commune or len(commune) > 60):
        return None
    for row in histories:
        old, new = _money(row.get("monto_anterior")), _money(row.get("monto_nuevo"))
        changed_at = _date(row.get("fecha"))
        if (changed_at is not None and old is not None and new is not None
            and 2000 <= new <= 5000 and old > new
            and abs(current - new) < 0.01):
            return {"code": code, "old_uf": old, "new_uf": new,
                    "changed_at": changed_at, "commune": commune}
    return None


def _self_declared_name(lead: dict) -> str | None:
    """Only user-supplied explicit introduction, never a guessed CRM name."""
    for msg in reversed((lead.get("messages") or [])[-80:]):
        if msg.get("role") != "user":
            continue
        match = NAME.search(str(msg.get("content") or "")[:500])
        if match:
            return match.group(1).capitalize()
    return None


def _excluded_by_customer(lead: dict) -> bool:
    for msg in (lead.get("messages") or []):
        if msg.get("role") == "user":
            content = str(msg.get("content") or "").casefold()
            content = content.translate(str.maketrans("áéíóúñ", "aeioun"))
            if STOP.search(content):
                return True
    return False


def _masked(phone: str) -> str:
    return "+56 •••• " + phone[-4:]


def _identity_unique(db: Any, phone: str, lead_id: Any) -> bool:
    variants = phone_identity_variants(phone)
    matches = list(db["leads"].find(
        {"$or": [{"phone": {"$in": variants}},
                 {"contact_phone": {"$in": variants}},
                 {"contact_phone_normalized": {"$in": variants}}]},
        {"_id": 1},
    ).limit(2))
    return len(matches) == 1 and str(matches[0].get("_id")) == str(lead_id)


def _recontacted_since_change(lead: dict, changed_at: datetime) -> bool:
    human = _date(lead.get("last_human_message_at"))
    if human is not None and human >= changed_at:
        return True
    for msg in (lead.get("messages") or []):
        when = _date(msg.get("timestamp"))
        if when and when >= changed_at and msg.get("role") in {"assistant", "user"}:
            return True
    return False


def _message(drop: dict, first_name: str | None) -> str:
    intro = f"¡Hola, {first_name}! ¿Cómo estás? 😊" if first_name else "¡Hola! ¿Cómo estás? 😊"
    return (
        f"{intro}\n\n"
        f"Te escribo de PROCASA porque el departamento de {drop['commune']} "
        f"bajó de precio: de UF {_uf(drop['old_uf'])} a UF {_uf(drop['new_uf'])}.\n\n"
        f"Te dejo el enlace por si quieres verlo 🏡\n"
        f"https://www.procasa.cl/{drop['code']}\n\n"
        "¿Te gustaría saber más o ir a verlo?"
    )


def preview_candidates(db: Any, *, code: str = CODE, max_review: int = MAX_REVIEW) -> dict:
    """Read only. Return masked internal previews, never a send-ready campaign."""
    if not (1 <= max_review <= MAX_REVIEW):
        raise ValueError("max_review must be between 1 and 3")
    drop = _verified_drop(db, code)
    if not drop:
        return {"status": "BLOCKED_UNVERIFIED_PRICE_DROP", "ready_to_send": False,
                "review": [], "excluded": {}, "property_code": code}
    projection = {
        "_id": 1, "phone": 1, "contact_phone": 1, "contact_phone_normalized": 1,
        "phone_is_synthetic": 1, "contact_identity_incomplete": 1,
        "prospecto.codigo": 1, "prospecto.operacion": 1,
        "created_at": 1, "last_human_message_at": 1,
        "lead_temperature_effective": 1, "ejecutivo_asignado": 1,
        "pipeline_stage": 1, "messages": 1,
    }
    leads = list(db["leads"].find(
        {"prospecto.codigo": code}, projection
    ).limit(MAX_PROPERTY_LEADS + 1))
    if len(leads) > MAX_PROPERTY_LEADS:
        return {"status": "BLOCKED_CANDIDATE_SCAN_LIMIT", "ready_to_send": False,
                "review": [], "excluded": {}, "property_code": code}
    # Priority first; never automatically substitute Cold for Hot.
    leads.sort(key=lambda x: (x.get("lead_temperature_effective") != "HOT",
                              str(x.get("created_at") or ""), str(x.get("_id"))))
    excluded: dict[str, int] = {}
    review: list[dict] = []
    seen: set[str] = set()
    for lead in leads:
        def exclude(reason: str) -> None:
            excluded[reason] = excluded.get(reason, 0) + 1
        if lead.get("lead_temperature_effective") != "HOT":
            exclude("not_hot")
            continue
        created_at = _date(lead.get("created_at"))
        if created_at is None or created_at >= drop["changed_at"]:
            exclude("missing_pre_drop_provenance")
            continue
        if (str((lead.get("prospecto") or {}).get("codigo") or "") != code
            or str((lead.get("prospecto") or {}).get("operacion") or "").lower() != "venta"):
            exclude("unverified_property_or_operation")
            continue
        raw_phone = str(lead.get("contact_phone_normalized") or lead.get("phone")
                        or lead.get("contact_phone") or "")
        phone = normalize_phone_strict(raw_phone)
        if (not has_real_phone(raw_phone) or phone is None
            or lead.get("phone_is_synthetic") is True
            or lead.get("contact_identity_incomplete") is True
            or not re.fullmatch(r"\+569\d{8}", phone)):
            exclude("invalid_or_incomplete_mobile")
            continue
        if phone in seen or not _identity_unique(db, phone, lead["_id"]):
            exclude("duplicate_identity")
            continue
        seen.add(phone)
        if _excluded_by_customer(lead):
            exclude("prior_opt_out")
            continue
        if _recontacted_since_change(lead, drop["changed_at"]):
            exclude("conversation_since_drop")
            continue
        if not str(lead.get("ejecutivo_asignado") or "").strip():
            exclude("no_current_owner")
            continue
        already = db[COLLECTION].find_one(
            {"phone_normalized": phone, "property_code": code,
             "status": {"$in": ["SENT", "SENDING", "DELIVERY_UNKNOWN"]}},
            {"_id": 1},
        )
        if already:
            exclude("previous_campaign_attempt")
            continue
        # Candidate is NOT automatically consented. Read-only staging cannot
        # replace a verified WhatsApp marketing opt-in or approved template.
        first_name = _self_declared_name(lead)
        review.append({
            "candidate_ref": hashlib.sha256(f"{code}|{str(lead['_id'])}".encode()).hexdigest()[:12],
            "phone_masked": _masked(phone),
            "temperature": "HOT",
            "current_owner": str(lead["ejecutivo_asignado"]),
            "property_code": code,
            "message_preview": _message(drop, first_name),
            "marketing_consent": "UNVERIFIED",
            "template_approval": "UNVERIFIED",
            "followup_integration": "NOT_CONNECTED",
            "decision": "HOLD_FOR_MANUAL_REVIEW",
        })
        if len(review) >= max_review:
            break
    return {
        "status": "PREVIEW_ONLY",
        "ready_to_send": False,
        "property_code": code,
        "verified_price_change": {
            "old_uf": drop["old_uf"], "new_uf": drop["new_uf"],
            "date": drop["changed_at"].isoformat(), "commune": drop["commune"],
        },
        "review": review, "excluded": excluded,
        "checks_needed_before_any_real_send": [
            "Documented contact permission and approved WhatsApp template",
            "Identity and owner reconfirmation immediately before send",
            "Verified CRM handoff for positive replies, with no SLA regression",
            "Provider status and one-attempt idempotency",
            "Explicit release after a separate internal end-to-end test",
        ],
    }
