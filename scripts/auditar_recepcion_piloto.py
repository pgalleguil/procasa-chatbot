"""Diagnostico de solo lectura del ingreso de respuestas del piloto de rebajas.

Nunca envia WhatsApp, no llama al LLM, no inserta trabajos ni modifica leads.
No imprime telefonos completos, transcripciones, secretos o IDs del proveedor.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from dotenv import load_dotenv

if os.getenv("PRICE_DROP_ENV_FILE"):
    load_dotenv(Path(os.environ["PRICE_DROP_ENV_FILE"]).expanduser(), override=False)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatbot.storage import get_db
from config import Config

PILOT = "pilot_real_rebajas_20261109_v2"
CODES = {"16479", "6436", "6377"}
WEBHOOK_EXPECTED = "https://procasa-chatbot-yr8d.onrender.com/webhook"
RECEIVE_EVENTS = {"messages.received", "messages.upsert", "messages.personal.received"}
HTTP_TIMEOUT = (3, 12)


def _timestamp(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _clean_link(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme == "https" else None
    except ValueError:
        return None


def _session_webhook(detail):
    """Only safe, documented configuration indicators; missing == unknown."""
    cfg = detail.get("webhook") if isinstance(detail.get("webhook"), dict) else {}
    url = next((detail[k] for k in ("webhook_url", "webhookUrl") if isinstance(detail.get(k), str)), None)
    if not url:
        url = cfg.get("url") or cfg.get("webhook_url")
    enabled = next((detail[k] for k in ("webhook_enabled", "webhookEnabled", "enable_webhook", "enableWebhook")
                    if isinstance(detail.get(k), bool)), None)
    if enabled is None:
        enabled = next((cfg.get(k) for k in ("enabled", "is_enabled") if isinstance(cfg.get(k), bool)), None)
    events = next((detail.get(k) for k in ("webhook_events", "webhookEvents", "webhook_subscribed_events")
                   if isinstance(detail.get(k), (list, dict))), None)
    if events is None:
        events = cfg.get("events")
    if isinstance(events, dict):
        names = sorted(str(k).strip().lower() for k, v in events.items() if v is True)
    elif isinstance(events, list):
        names = sorted(str(x).strip().lower() for x in events if isinstance(x, str))
    else:
        names = None
    return {
        "webhook_url": _clean_link(url),
        "webhook_url_matches_production": (
            _clean_link(url) == WEBHOOK_EXPECTED if url else None
        ),
        "webhook_enabled": enabled,
        "subscribed_receive_events": sorted(RECEIVE_EVENTS.intersection(names)) if names is not None else None,
        "webhook_configuration_reported": bool(url or enabled is not None or names is not None),
        "safe_webhook_field_names": sorted(
            k for k in detail if ("webhook" in k.lower() or "event" in k.lower())
            and not any(secret in k.lower() for secret in ("secret", "token", "key", "auth"))
        )[:30],
    }


def _provider_state(*, http_get=requests.get):
    token = str(getattr(Config, "WASENDER_PERSONAL_ACCESS_TOKEN", "") or "").strip()
    base_url = str(getattr(Config, "WASENDER_BASE_URL", "") or "").rstrip("/")
    if not token or not base_url.startswith("https://"):
        return {"status": "UNAVAILABLE", "reason": "account_PAT_or_https_base_url_missing"}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    def get_data(url):
        response = http_get(url, headers=headers, timeout=HTTP_TIMEOUT)
        status = getattr(response, "status_code", None)
        if status != 200:
            raise RuntimeError(f"provider_http_{status}")
        body = response.json()
        if not isinstance(body, dict) or body.get("success") is False:
            raise RuntimeError("provider_response_invalid")
        return body.get("data")
    try:
        payload = get_data(base_url + "/whatsapp-sessions")
        sessions = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(sessions, list) or len(sessions) > 100:
            raise RuntimeError("provider_session_list_unexpected")
        result = []
        for session in sessions:
            if not isinstance(session, dict) or not session.get("id"):
                raise RuntimeError("provider_session_metadata_incomplete")
            item = get_data(base_url + "/whatsapp-sessions/"
                            + quote(str(session["id"]), safe=""))
            detail = item if isinstance(item, dict) else {}
            merged = dict(session)
            merged.update(detail)
            credential = str(merged.get("api_key") or merged.get("apiKey") or "")
            office_key = str(getattr(Config, "WASENDER_TOKEN", "") or "")
            matched = (credential == office_key) if credential and office_key else None
            result.append({
                "session_id_last4": str(session["id"])[-4:],
                "office_session_token_match": matched,
                "status": merged.get("status") if isinstance(merged.get("status"), str) else None,
                "logging_enabled": merged.get("log_messages") if isinstance(merged.get("log_messages"), bool) else None,
                **_session_webhook(merged),
            })
        return {"status": "READ_ONLY", "sessions": result, "sessions_count": len(result)}
    except Exception as exc:
        return {"status": "UNAVAILABLE", "reason": str(exc)[:120]}


def pilot_ingress_audit(db, *, provider_get=requests.get):
    docs = list(db["leads"].find(
        {"price_drop_v2.campaigns.campaign_id": PILOT},
        {"_id": 1, "phone": 1, "is_paused": 1, "conversation_owner": 1,
         "human_active": 1, "human_handoff_source": 1,
         "price_drop_v2.campaigns": 1, "messages.role": 1,
         "messages.timestamp": 1, "messages.provider_message_id": 1,
         "prospecto.nombre": 1},
    ).limit(5))
    if len(docs) != 3:
        return {"status": "BLOCKED_PILOT_SET_UNEXPECTED",
                "actual_count": len(docs), "database_mode": "READ_ONLY"}
    rows = []
    for d in docs:
        c = [c for c in (d.get("price_drop_v2") or {}).get("campaigns") or []
             if c.get("campaign_id") == PILOT]
        if len(c) != 1 or str(c[0].get("property_code") or "") not in CODES:
            return {"status": "BLOCKED_PILOT_SET_UNEXPECTED", "database_mode": "READ_ONLY"}
        campaign = c[0]
        sent = _timestamp(campaign.get("sent_at"))
        inbound_after_send = sum(1 for m in d.get("messages") or []
                                 if m.get("role") in {"user", "customer"}
                                 and sent and (_timestamp(m.get("timestamp")) or
                                               datetime(1970, 1, 1, tzinfo=timezone.utc)) >= sent)
        phone = str(d.get("phone") or "")
        inbound_jobs = db["chatbot_inbound_jobs"].count_documents({
            "kind": "inbound_job", "phone": phone,
            **({"received_at": {"$gte": sent}} if sent else {}),
        }) if phone else 0
        rows.append({
            "property_code": str(campaign["property_code"]),
            "lead_last6": str(d["_id"])[-6:],
            "campaign_status": campaign.get("status"),
            "provider_status": campaign.get("provider_status"),
            "campaign_reply_count": campaign.get("reply_count", 0),
            "message_inbound_after_send": inbound_after_send,
            "durable_queue_inbound_after_send": inbound_jobs,
            "is_paused": d.get("is_paused") if "is_paused" in d else None,
            "conversation_owner": d.get("conversation_owner"),
            "human_active": d.get("human_active"),
            "human_handoff_source": d.get("human_handoff_source"),
            "validated_name_available": bool((d.get("prospecto") or {}).get("nombre")),
        })
    latest = db["chatbot_inbound_jobs"].find_one(
        {"kind": "inbound_job"}, {"_id": 0, "received_at": 1},
        sort=[("received_at", -1)],
    ) or {}
    return {
        "status": "AUDITED_READ_ONLY", "database_mode": "READ_ONLY",
        "pilot_recipients": sorted(rows, key=lambda item: item["property_code"]),
        "latest_durable_inbound_utc": str(latest.get("received_at") or "NONE"),
        "provider_session_audit": _provider_state(http_get=provider_get),
        "note": ("This cannot prove a WhatsApp reply did not exist; "
                 "it proves whether it reached the canonical queue/lead."),
        "send_attempts": 0, "mongo_mutations": 0,
    }


if __name__ == "__main__":
    try:
        if not Config.MONGO_URI:
            raise RuntimeError("MONGO_URI_not_configured_use_PRICE_DROP_ENV_FILE")
        print(json.dumps(pilot_ingress_audit(get_db()), ensure_ascii=False,
                         default=str, indent=2))
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "reason": type(exc).__name__,
                          "detail": str(exc)[:100]}, ensure_ascii=False))
        sys.exit(1)
