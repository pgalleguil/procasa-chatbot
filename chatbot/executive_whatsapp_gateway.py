"""Unregistered HTTP adapter for passive executive WhatsApp auditing.

IMPORTANT: Nothing in this module is mounted in webhook.py or production.
A future opt-in integration must validate privacy, storage, and retry policy.
No API client and no outbound WhatsApp capability is imported here.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from fastapi import APIRouter, HTTPException, Request

from .executive_whatsapp_audit import (
    ExecutiveAuditError,
    ingest_upsert,
    verify_webhook_secret,
)

DEFAULT_MAX_REQUEST_BYTES = 64 * 1024
MAX_EVENT_MESSAGES = 50


@dataclass(frozen=True)
class SessionBinding:
    """The trusted configuration of an authorized executive WhatsApp session."""

    session_id: str
    executive_user_id: str
    webhook_secret: str

    def __post_init__(self) -> None:
        if not all((
            isinstance(self.session_id, str) and self.session_id.strip(),
            isinstance(self.executive_user_id, str) and self.executive_user_id.strip(),
            isinstance(self.webhook_secret, str) and len(self.webhook_secret) >= 16,
        )):
            raise ValueError("invalid_executive_session_binding")


def _no_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _validate_shape(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid_payload")
    if payload.get("event") != "messages.upsert":
        # Domain boundary: non-upsert event must never enter lead/chatbot pipeline.
        raise HTTPException(status_code=422, detail="unsupported_event")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="invalid_data")
    rows = data.get("messages")
    if isinstance(rows, list):
        if not rows or len(rows) > MAX_EVENT_MESSAGES:
            raise HTTPException(status_code=413, detail="message_batch_out_of_range")
    elif not isinstance(rows, dict):
        raise HTTPException(status_code=400, detail="invalid_messages")
    return payload


async def _bounded_body(request: Request, *, maximum: int) -> bytes:
    body = bytearray()
    async for part in request.stream():
        if len(body) + len(part) > maximum:
            raise HTTPException(status_code=413, detail="request_too_large")
        body.extend(part)
    return bytes(body)


def build_unmounted_executive_audit_router(
    *,
    session_bindings: Mapping[str, SessionBinding],
    db_factory: Callable[[], Any],
    enabled: bool = False,
    persist_enabled: bool = False,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
) -> APIRouter:
    """Return an isolated router WITHOUT registering it with the application.

    Both gates must be set deliberately to persist: enabled=True and
    persist_enabled=True. The default is no capture, no database access.
    session_alias is only an opaque lookup key; identity comes from the binding,
    never from a request parameter, body, or unsigned event field.

    This prototype ACKs only after synchronous durable commit (or known duplicate).
    Any unconfirmed writes return 503 for provider retry. Do not mount in
    production before verifying vendor retry behavior, consent and capacity.
    """
    if max_request_bytes < 1024 or max_request_bytes > 1024 * 1024:
        raise ValueError("unsafe_max_request_bytes")
    if not callable(db_factory):
        raise ValueError("db_factory_required")
    if any(
        not isinstance(alias, str) or not alias.strip()
        or not isinstance(binding, SessionBinding)
        for alias, binding in session_bindings.items()
    ):
        raise ValueError("invalid_session_registry")

    router = APIRouter(include_in_schema=False)

    @router.post("/internal/executive-whatsapp/{session_alias}/events")
    async def receive_executive_event(session_alias: str, request: Request) -> dict[str, Any]:
        if not enabled:
            raise HTTPException(status_code=404, detail="not_found")
        binding = session_bindings.get(session_alias)
        if binding is None:
            raise HTTPException(status_code=404, detail="not_found")
        try:
            verify_webhook_secret(
                request.headers.get("x-webhook-signature"),
                binding.webhook_secret,
            )
        except ExecutiveAuditError:
            raise HTTPException(status_code=401, detail="invalid_signature") from None

        media_type = request.headers.get("content-type", "").split(";", 1)[0].lower().strip()
        if media_type != "application/json":
            raise HTTPException(status_code=415, detail="json_required")

        try:
            payload = json.loads(
                (await _bounded_body(request, maximum=max_request_bytes)).decode("utf-8"),
                object_pairs_hook=_no_duplicate_json_keys,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid_json") from None
        payload = _validate_shape(payload)

        # No messages, phone numbers or secrets in response/logs. This endpoint
        # does not import any WhatsApp send or chatbot processing entrypoint.
        try:
            def run_sync_ingest() -> dict[str, Any]:
                # Even creation of a sync PyMongo client must stay off the
                # request event loop. No WhatsApp office workers are invoked.
                return ingest_upsert(
                    db_factory(), payload,
                    session_id=binding.session_id,
                    executive_user_id=binding.executive_user_id,
                    signature_header=request.headers.get("x-webhook-signature"),
                    webhook_secret=binding.webhook_secret,
                    commit=bool(persist_enabled),
                )

            result = await asyncio.to_thread(run_sync_ingest)
        except ExecutiveAuditError:
            raise HTTPException(status_code=400, detail="invalid_message") from None
        except Exception:
            # Fail closed: non-2xx lets WaSender retry instead of losing an event.
            # Do not echo the exception, which might contain private data.
            raise HTTPException(status_code=503, detail="audit_ingest_unavailable") from None
        # Never acknowledge an authenticated provider event as durable if a
        # known lead's write was not confirmed. With no separate queue the
        # provider's retry mechanism is the at-least-once delivery boundary.
        retryable = {
            "retryable_write_unconfirmed",
            "invalid_bson_message",
            "message_too_large_for_lead",
            "unverified_original_timestamp",
        }
        if persist_enabled and any(
            result["results"].get(key, 0) for key in retryable
        ):
            raise HTTPException(status_code=503, detail="audit_persistence_not_confirmed")
        return {
            "accepted": True,
            "mode": result["mode"],
            "accepted_events": result["accepted_events"],
            "results": result["results"],
        }

    return router
