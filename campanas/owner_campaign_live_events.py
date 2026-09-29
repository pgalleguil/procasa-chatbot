"""Signed per-property tracking and post-persistence notifications."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from pymongo import MongoClient

from config import Config


LIVE_ACTIONS = {
    "aceptar_rebaja": "price_authorized",
    "contactar_ejecutivo": "advisor_review_requested",
    "ver_informe": "report_opened",
}


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _secret() -> str:
    return os.getenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "")


def issue_live_token(*, campaign_id: str, property_code: str, action: str, recipient: str, document_type: str | None = None, expires_at: int) -> str:
    secret = _secret()
    if not secret or not campaign_id or "test" in campaign_id.casefold() or action not in {*LIVE_ACTIONS, "cta_clicked", "price_confirm_page_opened"}:
        raise ValueError("production_action_token_not_configured")
    payload = {
        "campaign_id": campaign_id,
        "property_code": str(property_code),
        "action": action,
        "recipient": str(recipient).strip().casefold(),
        "exp": int(expires_at),
        "test_mode": False,
        "event_id": uuid4().hex,
    }
    if action == "ver_informe":
        if document_type not in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}:
            raise ValueError("invalid_live_document_type")
        payload["document_type"] = document_type
    encoded = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"p1.{encoded}.{signature}"


def verify_live_token(token: str, *, campaign_id: str, property_code: str, recipient: str, action: str, now: datetime | None = None) -> dict[str, Any] | None:
    payload = decode_live_token(token, now=now)
    if payload is None:
        return None
    if (
        payload.get("campaign_id") != campaign_id
        or str(payload.get("property_code")) != str(property_code)
        or payload.get("recipient") != str(recipient).strip().casefold()
        or payload.get("action") != action
    ):
        return None
    return payload


def decode_live_token(token: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    secret = _secret()
    if not secret or not token:
        return None
    try:
        version, encoded, supplied = token.split(".", 2)
        expected = _b64(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
        if version != "p1" or not hmac.compare_digest(supplied, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        expiry = datetime.fromtimestamp(int(payload["exp"]), timezone.utc)
        current = now or datetime.now(timezone.utc)
        if not isinstance(payload, dict) or payload.get("test_mode") is not False or expiry <= current:
            return None
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, OverflowError):
        return None


def persist_live_event(
    db: Any, claims: Mapping[str, Any], *, event: str, action: str,
    details: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    campaign_id = str(claims["campaign_id"])
    code = str(claims["property_code"])
    event_id = hashlib.sha256(
        f"{claims.get('event_id') or uuid4().hex}|{event}".encode("utf-8")
    ).hexdigest()
    event_at = datetime.now(timezone.utc)
    payload = {
        "event_id": event_id,
        "event_at": event_at,
        "event": event,
        "action": action,
        "property_code": code,
        "campaign_id": campaign_id,
    }
    if details:
        payload.update(dict(details))
    ledger = db[Config.COLLECTION_CAMPANAS_LOG]
    key = f"{campaign_id}:{code}"
    # An authorization is monotonic. The atomic update is scoped to one
    # campaign/property row and cannot change another property owner's state.
    query: dict[str, Any] = {"_id": key, "campaign_id": campaign_id, "property_code": code, "events.event_id": {"$ne": event_id}}
    update: dict[str, Any] = {"$push": {"events": payload}}
    if event == "price_authorized":
        query["authorization_status"] = {"$ne": "PRICE_AUTHORIZED"}
        update["$set"] = {
            "authorization_status": "PRICE_AUTHORIZED",
            "price_authorized_at": event_at,
            "selected_adjustment_type": payload.get("selected_adjustment_type"),
            "selected_adjustment_pct": payload.get("selected_adjustment_pct"),
            "selected_price": payload.get("selected_price"),
            "selected_price_clp": payload.get("selected_price_clp"),
            "authorized_price": payload.get("selected_price"),
        }
    elif event == "advisor_review_requested":
        cutoff = event_at - timedelta(hours=24)
        query["$or"] = [
            {"advisor_review_requested_at": {"$exists": False}},
            {"advisor_review_requested_at": {"$lte": cutoff}},
        ]
        update["$set"] = {"advisor_review_requested_at": event_at}
    elif event == "cta_clicked":
        update["$set"] = {"last_cta_clicked_at": event_at}
    elif event == "price_confirm_page_opened":
        update["$set"] = {"last_price_confirm_page_opened_at": event_at}
    elif event == "report_opened":
        update["$set"] = {"last_report_opened_at": event_at}
    result = ledger.update_one(query, update)
    if result.modified_count == 1:
        stored = ledger.find_one({"_id": key}) or {}
        return stored, True
    existing = ledger.find_one({"_id": key, "events.event_id": event_id}) or ledger.find_one({"_id": key})
    if not existing:
        raise LookupError("prepared_campaign_row_missing")
    return existing, False


def notify_internal_after_persist(stored: Mapping[str, Any], *, event: str) -> bool:
    """Send internal email only after the event is present in the ledger."""
    try:
        from .email_service import enviar_notificacion_owner_campaign

        return bool(enviar_notificacion_owner_campaign(
            owner_name=str(stored.get("owner_name") or "Sin nombre"),
            owner_email=str(stored.get("owner_email") or ""),
            property_code=str(stored.get("property_code") or ""),
            current_price=stored.get("current_price"),
            recommended_price=stored.get("recommended_price"),
            adjustment_pct=stored.get("recommended_adjustment_pct"),
            selected_adjustment_type=stored.get("selected_adjustment_type"),
            selected_adjustment_pct=stored.get("selected_adjustment_pct"),
            selected_price=stored.get("selected_price"),
            action=event,
            executive_name=str(stored.get("executive_name") or ""),
            executive_email=str(stored.get("executive_email") or ""),
            boss_cc=str(stored.get("boss_cc") or ""),
        ))
    except Exception:
        return False


def _event_identifier(claims: Mapping[str, Any], event: str) -> str:
    return hashlib.sha256(f"{claims.get('event_id')}|{event}".encode()).hexdigest()


def record_notification_result(db: Any, *, campaign_id: str, property_code: str, event: str, channel: str, result: Mapping[str, Any]) -> None:
    event_id = str(result.get("event_id") or "")
    field = f"notifications.{channel}.{event}"
    db[Config.COLLECTION_CAMPANAS_LOG].update_one(
        {"_id": f"{campaign_id}:{property_code}", "campaign_id": campaign_id, "property_code": property_code},
        {"$set": {
            f"{field}.status": str(result.get("status") or "unknown"),
            f"{field}.attempted_at": datetime.now(timezone.utc),
            f"{field}.provider_message_id": result.get("provider_message_id"),
            f"{field}.event_id": event_id,
        }},
    )


def _campaign_whatsapp_text(row: Mapping[str, Any], event: str, event_at: datetime) -> str:
    operation = str(row.get("operation") or "VENTA").upper()
    monthly = operation == "ARRIENDO"
    unit = "UF/mes" if monthly else "UF"
    clp_unit = "CLP/mes" if monthly else "CLP"

    def price(value: Any, *, clp: bool = False) -> str:
        if value in (None, ""):
            return "No disponible"
        try:
            number = float(value)
            rendered = f"{number:,.0f}".replace(",", ".")
        except (TypeError, ValueError):
            rendered = str(value)
        return ("$ " + rendered + " " + clp_unit) if clp else (rendered + " " + unit)

    current = price(row.get("current_price"))
    recommended = price(row.get("recommended_price"))
    selected = price(row.get("selected_price"))
    current_clp = row.get("current_price_clp")
    recommended_clp = row.get("recommended_price_clp")
    selected_clp = row.get("selected_price_clp")
    property_name = str(row.get("property_type") or "Propiedad")
    commune = str(row.get("commune") or "Comuna no disponible")
    owner = str(row.get("owner_name") or "Sin nombre")
    executive = str(row.get("executive_name") or "Sin ejecutivo")
    event_text = event_at.astimezone(timezone.utc).strftime("%d-%m-%Y %H:%M UTC")
    if event == "price_authorized":
        lines = [
            "✅ AJUSTE DE PRECIO CONFIRMADO", "",
            f"Código: {row.get('property_code')}", f"{property_name} · {commune}",
            f"Propietario: {owner}", "", f"Precio actual: {current}",
            f"Recomendación PROCASA: {row.get('recommended_adjustment_pct')}% → {recommended}",
            f"Ajuste aceptado: {row.get('selected_adjustment_pct')}% · {row.get('selected_adjustment_type')}",
            f"Nuevo precio autorizado: {selected}", f"Ejecutivo responsable: {executive}",
            f"Fecha: {event_text}", f"Campaña: {row.get('campaign_id')}",
        ]
        if current_clp is not None:
            lines.insert(7, f"Precio actual: {price(current_clp, clp=True)}")
        if recommended_clp is not None:
            lines.insert(9, f"Recomendación PROCASA CLP: {price(recommended_clp, clp=True)}")
        if selected_clp is not None:
            lines.insert(12, f"Nuevo precio autorizado CLP: {price(selected_clp, clp=True)}")
        return "\n".join(lines)
    return "\n".join([
        "📞 PROPIETARIO SOLICITA CONTACTO", "",
        f"Código: {row.get('property_code')}", f"{property_name} · {commune}",
        f"Propietario: {owner}", "", f"Precio actual: {current}",
        f"Recomendación PROCASA: {row.get('recommended_adjustment_pct')}% → {recommended}",
        f"Ejecutivo responsable: {executive}",
        "Motivo: Quiere revisar el ajuste antes de autorizarlo.",
        f"Fecha: {event_text}", f"Campaña: {row.get('campaign_id')}",
    ])


def _send_admin_whatsapp(row: Mapping[str, Any], event: str, event_at: datetime) -> dict[str, Any]:
    phone = str(getattr(Config, "OWNER_CAMPAIGN_ADMIN_WHATSAPP", "") or "").strip()
    if not phone or not Config.WASENDER_TOKEN:
        return {"status": "not_configured", "provider_message_id": None}
    try:
        import asyncio
        from chatbot.whatsapp_client import send_whatsapp_message_detailed

        result = asyncio.run(send_whatsapp_message_detailed(
            phone, _campaign_whatsapp_text(row, event, event_at),
        ))
        status = "sent" if result.get("success") else str(result.get("delivery_status") or "failed")
        return {"status": status, "provider_message_id": result.get("provider_message_id")}
    except Exception as exc:
        return {"status": "failed", "error_type": type(exc).__name__, "provider_message_id": None}


def notify_campaign_channels_after_persist(db: Any, *, stored: Mapping[str, Any], event: str, claims: Mapping[str, Any]) -> None:
    """Independent internal channels run only after the persisted event is read back."""
    campaign_id = str(claims["campaign_id"])
    code = str(claims["property_code"])
    event_id = _event_identifier(claims, event)
    event_at = next(
        (item.get("event_at") for item in stored.get("events", [])
         if isinstance(item, Mapping) and item.get("event_id") == event_id
         and isinstance(item.get("event_at"), datetime)),
        datetime.now(timezone.utc),
    )
    ledger = db[Config.COLLECTION_CAMPANAS_LOG]
    for channel in ("email", "admin_whatsapp"):
        state_path = f"notifications.{channel}.{event}.status"
        reserve = ledger.update_one(
            {
                "_id": f"{campaign_id}:{code}",
                "campaign_id": campaign_id,
                "property_code": code,
                state_path: {"$nin": ["sending", "sent", "accepted"]},
            },
            {"$set": {
                state_path: "sending",
                f"notifications.{channel}.{event}.event_id": event_id,
                f"notifications.{channel}.{event}.started_at": datetime.now(timezone.utc),
            }},
        )
        if reserve.modified_count != 1:
            continue
        if channel == "email":
            succeeded = notify_internal_after_persist(stored, event=event)
            result = {"status": "sent" if succeeded else "failed", "provider_message_id": None, "event_id": event_id}
        else:
            result = _send_admin_whatsapp(stored, event, event_at)
            result["event_id"] = event_id
        record_notification_result(
            db, campaign_id=campaign_id, property_code=code,
            event=event, channel=channel, result=result,
        )


def record_live_action(*, campaign_id: str, property_code: str, recipient: str, action: str, token: str) -> tuple[dict[str, Any], bool]:
    claims = verify_live_token(
        token, campaign_id=campaign_id, property_code=property_code,
        recipient=recipient, action=action,
    )
    if claims is None:
        raise ValueError("invalid_or_expired_campaign_token")
    if action == "aceptar_rebaja":
        # The action handler must persist the owner's explicit choice and price.
        # A generic event cannot safely authorize the recommendation by default.
        raise ValueError("price_authorization_requires_selected_adjustment")
    client = MongoClient(Config.MONGO_URI)
    try:
        db = client[Config.DB_NAME]
        stored, inserted = persist_live_event(
            db, claims, event=LIVE_ACTIONS[action], action=action,
        )
        # Read-after-write verifies persistence before the notification attempt.
        persisted = db[Config.COLLECTION_CAMPANAS_LOG].find_one({
            "_id": f"{campaign_id}:{property_code}",
            "events.event_id": _event_identifier(claims, LIVE_ACTIONS[action]),
        })
        if inserted and persisted and LIVE_ACTIONS[action] in {"price_authorized", "advisor_review_requested"}:
            notify_campaign_channels_after_persist(
                db, stored=persisted, event=LIVE_ACTIONS[action], claims=claims,
            )
        return stored, inserted
    finally:
        client.close()
