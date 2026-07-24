import asyncio
import logging
from typing import Any

import requests

from config import Config


logger = logging.getLogger(__name__)


def normalize_whatsapp_recipient(number: str) -> str:
    raw = str(number or "").strip()
    if "@" in raw:
        return raw
    digits = "".join(filter(str.isdigit, raw))
    if len(digits) == 9 and digits.startswith("9"):
        digits = "56" + digits
    if len(digits) == 11 and digits.startswith("569"):
        return "+" + digits
    raise ValueError("Destinatario de WhatsApp inválido")


def provider_recipient(number: str) -> str:
    normalized = normalize_whatsapp_recipient(number)
    return normalized if "@" in normalized else normalized.lstrip("+")


def mask_whatsapp_recipient(number: str) -> str:
    try:
        normalized = normalize_whatsapp_recipient(number)
    except ValueError:
        return "<destinatario inválido>"
    if "@" in normalized:
        return "grupo:***" + normalized[-8:]
    return f"+56 9 **** {normalized[-4:]}"


def _provider_message_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("msgId"),
        payload.get("messageId"),
        payload.get("message_id"),
        payload.get("id"),
        (payload.get("data") or {}).get("messageId") if isinstance(payload.get("data"), dict) else None,
        (payload.get("data") or {}).get("msgId") if isinstance(payload.get("data"), dict) else None,
        (payload.get("data") or {}).get("id") if isinstance(payload.get("data"), dict) else None,
        (payload.get("message") or {}).get("id") if isinstance(payload.get("message"), dict) else None,
    ]
    return next((str(value) for value in candidates if value), None)


WHATSAPP_STATUS_LABELS = {
    0: "failed",
    1: "pending",
    2: "sent",
    3: "delivered",
    4: "read",
    5: "played",
}


def normalize_provider_status(value) -> str:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, int):
        return WHATSAPP_STATUS_LABELS.get(value, "unknown")
    text = str(value or "").strip().lower()
    aliases = {"in_progress": "pending", "queued": "pending", "error": "failed"}
    return aliases.get(text, text or "unknown")


async def get_whatsapp_message_status(provider_message_id: str) -> dict:
    if not provider_message_id:
        return {"delivery_status": "unknown", "provider_message_id": None}
    url = f"{Config.WASENDER_BASE_URL}/messages/{provider_message_id}/info"
    headers = {"Authorization": f"Bearer {Config.WASENDER_TOKEN}"}
    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers, timeout=15)
        body = response.json() if response.content else {}
        data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
        status = normalize_provider_status((data or {}).get("status") if isinstance(data, dict) else None)
        return {
            "delivery_status": status,
            "provider_message_id": str(provider_message_id),
            "http_status": response.status_code,
        }
    except Exception as exc:
        logger.warning(
            "[WHATSAPP_STATUS] provider_message_id=%s error_type=%s",
            provider_message_id,
            type(exc).__name__,
        )
        return {"delivery_status": "unknown", "provider_message_id": str(provider_message_id)}


async def wait_for_whatsapp_delivery(provider_message_id: str, *, timeout_seconds=30) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last = {"delivery_status": "pending", "provider_message_id": str(provider_message_id)}
    while asyncio.get_running_loop().time() < deadline:
        last = await get_whatsapp_message_status(provider_message_id)
        if last.get("delivery_status") in {"delivered", "read", "played", "failed"}:
            return last
        await asyncio.sleep(3)
    return last


async def send_whatsapp_message_detailed(number: str, text: str) -> dict:
    """Envía por WASender y retorna metadatos seguros de aceptación del proveedor."""
    if not text:
        return {"success": False, "delivery_status": "rejected_empty_message", "provider_message_id": None}
    if not number or number in ("+56900000000", "56900000000"):
        logger.error("[WHATSAPP_SEND] rejected_placeholder phone=%s", str(number)[:15])
        return {"success": False, "delivery_status": "rejected_placeholder", "provider_message_id": None,
                "http_status": None, "provider_called": False}

    clean = provider_recipient(number)
    masked = mask_whatsapp_recipient(number)
    url = f"{Config.WASENDER_BASE_URL}/send-message"
    payload = {"to": clean, "text": text}
    headers = {
        "Authorization": f"Bearer {Config.WASENDER_TOKEN}",
        "Content-Type": "application/json",
    }

    last_status = None
    for attempt in (1, 2):
        try:
            response = await asyncio.to_thread(
                requests.post, url, json=payload, headers=headers, timeout=15
            )
            last_status = response.status_code
            try:
                body = response.json()
            except ValueError:
                body = {}
            success = response.status_code == 200 and body.get("success", True) is not False
            if success:
                message_id = _provider_message_id(body)
                data = body.get("data") if isinstance(body.get("data"), dict) else {}
                provider_status = normalize_provider_status(data.get("status") or body.get("status"))
                logger.info(
                    "[WHATSAPP_SEND] recipient=%s status=accepted provider_message_id=%s attempt=%s",
                    masked,
                    message_id or "unavailable",
                    attempt,
                )
                return {
                    "success": True,
                    "delivery_status": provider_status if provider_status != "unknown" else "accepted",
                    "provider_message_id": message_id,
                    "http_status": response.status_code,
                }
            logger.warning(
                "[WHATSAPP_SEND] recipient=%s status=failed http_status=%s attempt=%s",
                masked,
                response.status_code,
                attempt,
            )
        except Exception as exc:
            logger.error(
                "[WHATSAPP_SEND] recipient=%s status=exception attempt=%s error_type=%s",
                masked,
                attempt,
                type(exc).__name__,
            )
        if attempt == 1:
            await asyncio.sleep(2)

    return {
        "success": False,
        "delivery_status": "failed",
        "provider_message_id": None,
        "http_status": last_status,
    }


async def send_whatsapp_message(number: str, text: str) -> bool:
    """API compatible: conserva el booleano usado por los módulos existentes."""
    result = await send_whatsapp_message_detailed(number, text)
    return bool(result.get("success"))
