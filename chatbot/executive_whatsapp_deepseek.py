"""Opt-in DeepSeek JSON transport for the *independent* commercial audit.

NOT imported by the office chatbot, and NEVER called from the WhatsApp webhook.
The only input should be build_private_model_request()'s minimized payload,
after a separate privacy/security authorization. No customer data is logged.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

DEFAULT_MODEL = "deepseek-flash"
ALLOWED_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})
MAX_AUDIT_OUTPUT_TOKENS = 900


def make_deepseek_audit_transport(
    *,
    api_key: str,
    privacy_authorization: str = "",
    model: str = DEFAULT_MODEL,
    client: Any | None = None,
) -> Callable[[Mapping[str, Any]], str]:
    """Build a transport for explicit supervised invocations only.

    Never create a network client before verifying the privacy gate.
    Passing a fake OpenAI-compatible client enables fully offline unit tests.
    """
    if privacy_authorization != "EXPLICITLY_APPROVED_DATA_PROCESSING":
        raise PermissionError("commercial_chat_privacy_review_required")
    if not isinstance(api_key, str) or len(api_key.strip()) < 12:
        raise ValueError("deepseek_api_key_required")
    if model not in ALLOWED_MODELS:
        raise ValueError("unsupported_deepseek_audit_model")
    if client is None:
        from openai import OpenAI
        client = OpenAI(
            api_key=api_key.strip(),
            base_url="https://api.deepseek.com",
            timeout=20.0,
            max_retries=0,
        )

    def call(request: Mapping[str, Any]) -> str:
        if request.get("task") != "commercial_audit_candidate_only":
            raise ValueError("audit_only_model_transport")
        if request.get("no_automatic_actions") is not True:
            raise ValueError("model_actions_must_be_disabled")
        messages = request.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > 300:
            raise ValueError("invalid_audit_transcript")
        system = request.get("system")
        if not isinstance(system, str) or "JSON" not in system.upper():
            raise ValueError("json_audit_system_prompt_required")
        payload = json.dumps(
            {
                "coverage_complete": request.get("coverage_complete") is True,
                "messages": messages,
                "required_schema": request.get("required_schema"),
                "instruction": "Return exactly one JSON object; do not call tools.",
            },
            ensure_ascii=False, separators=(",", ":"),
        )
        if len(payload) > 40_000:
            raise ValueError("audit_model_payload_too_large")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": payload},
            ],
            thinking={"type": "disabled"},
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=MAX_AUDIT_OUTPUT_TOKENS,
            stream=False,
        )
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise ValueError("audit_model_incomplete_completion")
        body = response.choices[0].message.content
        if not isinstance(body, str) or not body.strip():
            raise ValueError("audit_model_empty_completion")
        # Structural validation remains in validate_audit_result, not here.
        return body

    return call
