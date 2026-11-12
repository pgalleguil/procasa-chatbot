"""Strictly transient probe for an authorized SINGLE executive WaSender session.

It emits ONLY counts; does not import any MongoDB, chatbot, send API, LLM or
storage code. Use it to verify manual outgoing messages before granting access
to any real client chat data.
"""
from __future__ import annotations

import json
from typing import Mapping

from fastapi import APIRouter, HTTPException, Request

from .executive_whatsapp_audit import ExecutiveAuditError, normalize_upsert, verify_webhook_secret
from .executive_whatsapp_gateway import (
    SessionBinding,
    _bounded_body,
    _no_duplicate_json_keys,
    _validate_shape,
    DEFAULT_MAX_REQUEST_BYTES,
)

def build_probe_only_router(
    session_bindings: Mapping[str, SessionBinding],
    *,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
) -> APIRouter:
    if len(session_bindings) != 1:
        raise ValueError("probe_requires_exactly_one_session")
    router = APIRouter(include_in_schema=False)

    @router.post("/internal/executive-whatsapp/{session_alias}/events")
    async def probe(session_alias: str, request: Request):
        binding = session_bindings.get(session_alias)
        if binding is None:
            raise HTTPException(status_code=404, detail="not_found")
        try:
            verify_webhook_secret(
                request.headers.get("x-webhook-signature"), binding.webhook_secret
            )
        except ExecutiveAuditError:
            raise HTTPException(status_code=401, detail="invalid_signature") from None
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise HTTPException(status_code=415, detail="json_required")
        try:
            payload = json.loads(
                (await _bounded_body(request, maximum=max_request_bytes)).decode("utf-8"),
                object_pairs_hook=_no_duplicate_json_keys,
            )
        except (ValueError, UnicodeError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="invalid_json") from None
        payload = _validate_shape(payload)
        records, skipped = normalize_upsert(
            payload,
            session_id=binding.session_id,
            executive_user_id=binding.executive_user_id,
        )
        # No PII, message IDs, body, phone, session ID, sender labels or
        # raw JSON in response. No persistence or model invocation.
        result = {
            "probe_only": True,
            "total_valid": len(records),
            "incoming_client_messages": sum(
                1 for item in records if item["actor_type"] == "client"
            ),
            "outgoing_executive_messages": sum(
                1 for item in records if item["actor_type"] == "executive"
            ),
            "unmatched_or_unsupported": len(skipped),
            "original_message_time_present": sum(
                1 for item in records if item["timestamp_origin"] == "message_timestamp"
            ),
            "payload_contents_stored": False,
            "mongo_connections": 0,
            "outbound_messages_sent": 0,
        }
        return result

    return router
