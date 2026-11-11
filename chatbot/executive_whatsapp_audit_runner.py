"""Inert DeepSeek adapter contract for commercial executive audit.

Requires a caller-injected model transport, no API keys, network clients,
MongoDB writes, or chatbot imports. Not registered or scheduled anywhere.

This only creates a candidate audit. A human must still review it.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping

from .executive_whatsapp_classifier import (
    prepare_audit_input,
    validate_audit_result,
)

MAX_AUDIT_BODY_CHARACTERS = 40_000
_PHONE = re.compile(r"(?<!\d)(?:\+?\s*56[\s.\-]*)?9(?:[\s.\-]*\d){8}(?!\d)")
_EMAIL = re.compile(r"(?<![\w@])[\w.%+\-]+@[\w.\-]+\.[A-Za-z]{2,}(?!\w)")
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def _scrub(text: str) -> str:
    # Conservative first-pass PII scrubbing, NOT a certification of anonymization.
    text = _URL.sub("[URL]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    return _PHONE.sub("[PHONE]", text)


def _opaque_ref(source: Any) -> str:
    return hashlib.sha256(str(source or "").encode("utf-8")).hexdigest()[:16]


def build_private_model_request(
    lead: Mapping[str, Any], *,
    coverage_complete: bool = False,
    max_messages: int = 200,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (clean request, local validation context) without client identity.

    NOT to be invoked with real conversations before data-processing approvals.
    """
    if not 1 <= max_messages <= 300:
        raise ValueError("invalid_message_limit")
    prepared = prepare_audit_input(
        lead, coverage_complete=coverage_complete, max_messages=max_messages
    )
    clean_messages = []
    for msg in prepared["messages"]:
        content = msg.get("content") or ""
        if not isinstance(content, str):
            continue
        clean_messages.append({
            "timestamp": msg.get("timestamp"),
            "actor_type": msg.get("actor_type"),
            "source": msg.get("source"),
            "assignment_cycle": _opaque_ref(msg.get("assignment_cycle_id"))
            if msg.get("assignment_cycle_id") else None,
            "content": _scrub(content),
            "evidence_key": msg.get("evidence_key"),
        })
    request = {
        "system": prepared["system"],
        "task": "commercial_audit_candidate_only",
        "coverage_complete": False if not coverage_complete else True,
        "messages": clean_messages,
        "required_schema": prepared["required_schema"],
        "no_automatic_actions": True,
    }
    # Crucial: transcript truncation does not prove full historical coverage.
    # coverage_complete is an externally verified fact, never model-inferred.
    content = json.dumps(request, ensure_ascii=False)
    if len(content) > MAX_AUDIT_BODY_CHARACTERS:
        raise ValueError("audit_prompt_exceeds_privacy_budget")
    return request, prepared


def evaluate_with_injected_model(
    lead: Mapping[str, Any],
    *,
    model_call: Callable[[dict[str, Any]], str | Mapping[str, Any]],
    coverage_complete: bool = False,
    max_messages: int = 200,
) -> dict[str, Any]:
    """Offline orchestration: caller must inject a controlled, consented model.

    No automatic campaign, no CRM/SLA update and no MongoDB write.
    """
    if not callable(model_call):
        raise ValueError("explicit_model_transport_required")
    request, context = build_private_model_request(
        lead, coverage_complete=coverage_complete, max_messages=max_messages
    )
    raw = model_call(request)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("model_returned_nonjson") from None
    if not isinstance(raw, Mapping):
        raise ValueError("model_returned_nonobject")
    validated = validate_audit_result(raw, context)
    return {
        "candidate": validated,
        "review_status": "PENDING_HUMAN_REVIEW",
        "sends_performed": 0,
        "writes_performed": 0,
        "classification_persisted": False,
        "customer_campaign_eligible": False,
    }
