"""Fail-closed, test-only owner campaign CTA handling.

Only signed links for the fixed test campaign and test recipient are accepted.
Actions append audit events to the existing ``conversation_events`` collection;
they never write to contacts, price_updates, or the property portfolio.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi.responses import HTMLResponse

from config import Config


TEST_RECIPIENT = "pgalleguillos@procasa.cl"
TEST_CAMPAIGN_ID = "owner_price_campaign_test_20260923"
REPORT_ACTION = "ver_informe"
ACCEPT_PRICE_ACTION = "aceptar_nuevo_valor"
ADVISOR_ACTION = "revisar_con_mi_asesor"
ALLOWED_ACTIONS = frozenset({REPORT_ACTION, ACCEPT_PRICE_ACTION, ADVISOR_ACTION})
PROPERTY_COLLECTION = "universo_cartera_prop360"
EVENT_COLLECTION = "conversation_events"
LEDGER_COLLECTION = "ajuste_precio"
PROPERTY_CODE_RE = re.compile(r"^[0-9]{1,32}$")


class OwnerCampaignTestError(ValueError):
    """A test action did not meet the signed test-only authorization contract."""


def test_mode_enabled() -> bool:
    return os.getenv("OWNER_CAMPAIGN_TEST_MODE", "").strip().casefold() == "true"


def _test_secret() -> str:
    return os.getenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", "")


def issue_test_link_token(
    *,
    property_code: str,
    action: str,
    document_type: str | None = None,
    expires_in_seconds: int = 3600,
    now_epoch: int | None = None,
) -> str:
    """Create a short-lived signed link for a single test property/action."""
    if not test_mode_enabled():
        raise OwnerCampaignTestError("test_mode_disabled")
    secret = _test_secret()
    if not secret:
        raise OwnerCampaignTestError("test_token_secret_missing")
    code = str(property_code or "").strip()
    action_value = str(action or "").strip()
    if not PROPERTY_CODE_RE.fullmatch(code) or action_value not in ALLOWED_ACTIONS:
        raise OwnerCampaignTestError("test_link_claims_invalid")
    if not isinstance(expires_in_seconds, int) or not 60 <= expires_in_seconds <= 86400:
        raise OwnerCampaignTestError("test_link_ttl_invalid")
    claims: dict[str, Any] = {
        "campaign_id": TEST_CAMPAIGN_ID,
        "property_code": code,
        "action": action_value,
        "recipient": TEST_RECIPIENT,
        "test_mode": True,
        "exp": int(now_epoch if now_epoch is not None else time.time()) + expires_in_seconds,
    }
    if action_value == REPORT_ACTION:
        normalized_type = str(document_type or "").strip().upper()
        if normalized_type not in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}:
            raise OwnerCampaignTestError("test_report_type_invalid")
        claims["document_type"] = normalized_type
    elif document_type is not None:
        normalized_type = str(document_type).strip().upper()
        if normalized_type not in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}:
            raise OwnerCampaignTestError("test_report_type_invalid")
        claims["document_type"] = normalized_type

    encoded = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    return f"t1.{encoded}.{signature}"


def verify_test_link_token(
    token: str,
    *,
    allowed_actions: frozenset[str] = ALLOWED_ACTIONS,
    now_epoch: int | None = None,
) -> dict[str, Any] | None:
    """Verify a test token without trusting any caller-supplied routing data."""
    secret = _test_secret()
    if not test_mode_enabled() or not secret or not token:
        return None
    try:
        version, encoded, supplied_signature = token.split(".", 2)
        if version != "t1":
            return None
        expected_signature = base64.urlsafe_b64encode(
            hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        ).rstrip(b"=").decode("ascii")
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(claims, dict):
            return None
        code = str(claims.get("property_code") or "")
        action = str(claims.get("action") or "")
        now = int(now_epoch if now_epoch is not None else time.time())
        if (
            claims.get("campaign_id") != TEST_CAMPAIGN_ID
            or claims.get("test_mode") is not True
            or str(claims.get("recipient") or "").strip().casefold() != TEST_RECIPIENT
            or action not in allowed_actions
            or not PROPERTY_CODE_RE.fullmatch(code)
            or int(claims.get("exp") or 0) <= now
        ):
            return None
        if action == REPORT_ACTION and claims.get("document_type") not in {
            "INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"
        }:
            return None
        return claims
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError, OverflowError):
        return None


def _database(db: Any = None):
    if db is not None:
        return db
    from chatbot.storage import get_db

    return get_db()


def _test_ledger(db: Any, property_code: str) -> Mapping[str, Any] | None:
    return db[LEDGER_COLLECTION].find_one(
        {
            "campaign_id": TEST_CAMPAIGN_ID,
            "property_code": property_code,
            "test_mode": True,
            "actual_recipient_email": TEST_RECIPIENT,
        }
    )


def _insert_test_event(
    db: Any,
    *,
    event_type: str,
    property_code: str,
    executive: str,
    token: str,
    event_at: datetime | None = None,
) -> str:
    if event_type not in {
        "cta_clicked", "price_authorized", "advisor_review_requested", "report_opened"
    }:
        raise OwnerCampaignTestError("test_event_type_invalid")
    event_at = event_at or datetime.now(timezone.utc)
    event_id = hashlib.sha256(
        f"{TEST_CAMPAIGN_ID}|{property_code}|{event_type}|{token}".encode("utf-8")
    ).hexdigest()
    collection = db[EVENT_COLLECTION]
    existing = collection.find_one({"event_id": event_id}, {"event_id": 1})
    if existing:
        return str(existing["event_id"])
    collection.insert_one(
        {
            "event_id": event_id,
            "event_type": event_type,
            "campaign_id": TEST_CAMPAIGN_ID,
            "property_code": property_code,
            "executive": executive,
            "event_at": event_at,
            "test_mode": True,
            "source": "owner_campaign_test",
        }
    )
    return event_id


def record_test_report_opened(property_code: str, *, token: str, db: Any = None) -> str:
    """Append only a report_opened event for a previously registered test send."""
    if not test_mode_enabled():
        raise OwnerCampaignTestError("test_mode_disabled")
    code = str(property_code or "").strip()
    if not PROPERTY_CODE_RE.fullmatch(code):
        raise OwnerCampaignTestError("property_code_invalid")
    database = _database(db)
    ledger = _test_ledger(database, code)
    if not ledger:
        raise OwnerCampaignTestError("test_campaign_ledger_missing")
    executive = str(ledger.get("executive") or "").strip()
    if not executive:
        raise OwnerCampaignTestError("test_executive_missing")
    if not token:
        raise OwnerCampaignTestError("test_report_token_missing")
    return _insert_test_event(
        database,
        event_type="report_opened",
        property_code=code,
        executive=executive,
        # The helper hashes this value into event_id and never persists it.
        token=token,
    )


def _live_price_snapshot(db: Any, property_code: str) -> tuple[str, dict[str, Any]]:
    property_doc = db[PROPERTY_COLLECTION].find_one(
        {"codigo": property_code},
        {
            "_id": 0,
            "codigo": 1,
            "tipo_operacion.tipo": 1,
            "tipo_operacion.venta": 1,
            "tipo_operacion.arriendo": 1,
            "tipo_operacion.precio_venta.precio_uf": 1,
            "tipo_operacion.precio_venta.precio_clp": 1,
            "tipo_operacion.precio_arriendo.precio_uf": 1,
            "tipo_operacion.precio_arriendo.precio_clp": 1,
            "estado.ejecutivo": 1,
        },
    )
    if not isinstance(property_doc, Mapping) or str(property_doc.get("codigo") or "") != property_code:
        raise OwnerCampaignTestError("test_property_not_found")
    operation_data = property_doc.get("tipo_operacion") or {}
    operation_text = str(operation_data.get("tipo") or "").casefold()
    sale = "venta" in operation_text
    rent = "arriend" in operation_text
    if sale == rent:
        sale = operation_data.get("venta") is True
        rent = operation_data.get("arriendo") is True
    if sale == rent:
        raise OwnerCampaignTestError("test_property_operation_ambiguous")
    operation = "VENTA" if sale else "ARRIENDO"
    price_key = "precio_venta" if sale else "precio_arriendo"
    price = operation_data.get(price_key) or {}
    if not isinstance(price, Mapping):
        raise OwnerCampaignTestError("test_live_price_missing")
    snapshot = {key: price.get(key) for key in ("precio_uf", "precio_clp")}
    if snapshot["precio_uf"] is None:
        raise OwnerCampaignTestError("test_live_price_missing")
    return operation, snapshot


def _valid_lower_target(ledger: Mapping[str, Any], current_uf: Any) -> bool:
    if ledger.get("evidence_segment") != "PRICE_AUTHORIZATION_READY":
        return False
    try:
        current = float(current_uf)
        recommended = float(ledger.get("display_recommended_price"))
    except (TypeError, ValueError):
        return False
    return math.isfinite(current) and math.isfinite(recommended) and 0 < recommended < current


def process_test_action(token: str, *, db: Any = None) -> dict[str, Any]:
    claims = verify_test_link_token(
        token,
        allowed_actions=frozenset({ACCEPT_PRICE_ACTION, ADVISOR_ACTION}),
    )
    if claims is None:
        raise OwnerCampaignTestError("test_action_token_invalid")
    code = str(claims["property_code"])
    action = str(claims["action"])
    database = _database(db)
    ledger = _test_ledger(database, code)
    if not ledger:
        raise OwnerCampaignTestError("test_campaign_ledger_missing")
    executive = str(ledger.get("executive") or "").strip()
    if not executive:
        raise OwnerCampaignTestError("test_executive_missing")

    price_before = price_after = None
    if action == ACCEPT_PRICE_ACTION:
        if ledger.get("cta_type") != "PRICE_AUTHORIZATION":
            raise OwnerCampaignTestError("price_authorization_not_allowed")
        operation, price_before = _live_price_snapshot(database, code)
        if operation != "VENTA" or not _valid_lower_target(ledger, price_before.get("precio_uf")):
            raise OwnerCampaignTestError("price_authorization_evidence_invalid")
    elif action == ADVISOR_ACTION and ledger.get("cta_type") not in {
        "ADVISOR_REVIEW", "PRICE_AUTHORIZATION", "REPORT_ONLY"
    }:
        raise OwnerCampaignTestError("advisor_review_not_allowed")

    clicked_id = _insert_test_event(
        database,
        event_type="cta_clicked",
        property_code=code,
        executive=executive,
        token=token,
    )
    event_type = "price_authorized" if action == ACCEPT_PRICE_ACTION else "advisor_review_requested"
    outcome_id = _insert_test_event(
        database,
        event_type=event_type,
        property_code=code,
        executive=executive,
        token=token,
    )

    if action == ACCEPT_PRICE_ACTION:
        operation_after, price_after = _live_price_snapshot(database, code)
        if operation_after != operation or price_after != price_before:
            raise OwnerCampaignTestError("test_price_mutation_detected")
    return {
        "campaign_id": TEST_CAMPAIGN_ID,
        "property_code": code,
        "event": event_type,
        "event_ids": [clicked_id, outcome_id],
        "test_mode": True,
        "live_price_before": price_before,
        "live_price_after": price_after,
        "test_price_mutation": False,
    }


def handle_test_action(token: str, *, db: Any = None) -> HTMLResponse:
    try:
        result = process_test_action(token, db=db)
    except OwnerCampaignTestError:
        return HTMLResponse(
            "<main><h1>Acción de prueba no disponible</h1><p>Solicita un nuevo enlace a tu asesor.</p></main>",
            status_code=404,
        )
    except Exception:
        return HTMLResponse("<main><h1>No pudimos registrar la prueba</h1></main>", status_code=503)
    if result["event"] == "price_authorized":
        title = "Autorización de prueba registrada"
        message = "La autorización quedó registrada en modo de prueba. El precio publicado no fue modificado."
    else:
        title = "Solicitud de revisión registrada"
        message = "Tu solicitud quedó registrada en modo de prueba."
    return HTMLResponse(
        f"<main><h1>{title}</h1><p>{message}</p></main>",
        status_code=200,
        headers={"Cache-Control": "private, no-store"},
    )
