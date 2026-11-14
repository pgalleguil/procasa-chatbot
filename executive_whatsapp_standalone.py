"""Independent opt-in FastAPI app for ONE executive WhatsApp test session.

NEVER imported by the office chatbot or webhook.py. Deploy as a *separate*
service only after security/privacy review; default import exposes health only.
No send-message API or chatbot processing is imported.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Mapping

from fastapi import FastAPI

from chatbot.executive_whatsapp_gateway import (
    SessionBinding,
    build_unmounted_executive_audit_router,
)
from chatbot.executive_whatsapp_probe import build_probe_only_router

ENABLE_KEY = "PROCASA_EXEC_AUDIT_ENABLED"
PERSIST_KEY = "PROCASA_EXEC_AUDIT_PERSIST"
APPROVAL_KEY = "PROCASA_EXEC_AUDIT_PILOT_APPROVED"
BINDINGS_KEY = "PROCASA_EXEC_AUDIT_BINDINGS_JSON"
APPROVED_MARKER = "AUTHORIZED_ONE_PHONE_CANARY"


def _enabled(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == "true"


def _bindings(env: Mapping[str, str]) -> dict[str, SessionBinding]:
    raw = env.get(BINDINGS_KEY, "")
    try:
        config = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("invalid_binding_config") from None
    if not isinstance(config, dict) or len(config) != 1:
        raise ValueError("pilot_requires_exactly_one_executive_session")
    result = {}
    for alias, detail in config.items():
        if not isinstance(alias, str) or not alias.strip() or not isinstance(detail, dict):
            raise ValueError("invalid_session_registry")
        try:
            binding = SessionBinding(
                session_id=detail["session_id"],
                executive_user_id=detail["executive_user_id"],
                webhook_secret=detail["webhook_secret"],
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid_session_binding") from None
        result[alias] = binding
    return result


def _default_db_factory(env: Mapping[str, str]) -> Callable[[], Any]:
    uri = env.get("PROCASA_EXEC_AUDIT_MONGODB_URI", "")
    database = env.get("PROCASA_EXEC_AUDIT_DB", "URLS")
    if not isinstance(uri, str) or not uri.strip() or not database:
        raise ValueError("independent_mongodb_access_required")
    # A client for this service, NOT the office chatbot's shared connection.
    from pymongo import MongoClient

    client = MongoClient(
        uri,
        serverSelectionTimeoutMS=4000,
        connectTimeoutMS=4000,
        socketTimeoutMS=6000,
        maxPoolSize=5,
        retryWrites=True,
        appname="procasa-executive-whatsapp-audit-isolated",
    )
    db = client[database]
    return lambda: db


def create_app(
    *,
    env: Mapping[str, str] | None = None,
    db_factory: Callable[[], Any] | None = None,
) -> FastAPI:
    env = dict(os.environ if env is None else env)
    enabled = _enabled(env.get(ENABLE_KEY))
    persist = _enabled(env.get(PERSIST_KEY))
    probe_only = _enabled(env.get("PROCASA_EXEC_AUDIT_PROBE_ONLY"))
    if probe_only and persist:
        raise ValueError("probe_mode_must_not_persist")

    app = FastAPI(
        title="PROCASA Executive WhatsApp Audit (isolated)",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "service": "executive_whatsapp_audit_isolated",
            "capture_enabled": enabled,
            "storage_enabled": persist if enabled else False,
            "probe_only": probe_only if enabled else False,
            "office_chatbot_connected": False,
        }

    # The default service has no webhook route and no MongoDB connection.
    if not enabled:
        if persist:
            raise ValueError("persistence_cannot_be_enabled_without_capture")
        return app

    # Double/independent authorization: config cannot implicitly enable
    # capture by merely supplying a session secret.
    if env.get(APPROVAL_KEY) != APPROVED_MARKER:
        raise ValueError("one_phone_canary_approval_required")
    if persist and env.get("PROCASA_EXEC_AUDIT_PRIVACY_REVIEW") != "approved":
        raise ValueError("privacy_approval_required_for_persistence")

    registry = _bindings(env)
    if probe_only:
        # No Mongo client, no lead lookup, no WhatsApp messages persisted.
        # Use for the first manual inbound/outbound test only.
        app.include_router(build_probe_only_router(registry))
        return app
    db = db_factory if db_factory is not None else _default_db_factory(env)
    app.include_router(build_unmounted_executive_audit_router(
        session_bindings=registry,
        db_factory=db,
        enabled=True,
        persist_enabled=persist,
    ))
    return app


# Gunicorn/Uvicorn import exposes *no* receiving webhook until canary
# configuration is deliberately provided on a separate service.
app = create_app()
