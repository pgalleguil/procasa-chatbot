"""Offline shadow evaluator for a future DeepSeek Responses API migration.

This module is intentionally not imported by the production chatbot. It builds
and evaluates a Responses-style JSON Schema request so the experiment cannot
alter the current Chat Completions batching or delivery path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable


CHATBOT_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intencion": {"type": "string"},
        "respuesta_bot": {"type": "string"},
        "datos_extraidos": {"type": "object"},
    },
    "required": ["intencion", "respuesta_bot", "datos_extraidos"],
}


def build_shadow_responses_request(messages: list[dict], *, model: str) -> dict:
    """Build only the payload for the non-production shadow experiment."""
    return {
        "model": model,
        "input": messages,
        "reasoning": {"effort": "none"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "chatbot_turn",
                "strict": True,
                "schema": CHATBOT_RESPONSE_SCHEMA,
            }
        },
    }


@dataclass(frozen=True)
class ShadowMetrics:
    total: int
    valid_json: int
    empty_responses: int
    parse_errors: int

    @property
    def empty_rate(self) -> float:
        return self.empty_responses / self.total if self.total else 0.0

    @property
    def parse_error_rate(self) -> float:
        return self.parse_errors / self.total if self.total else 0.0


def evaluate_shadow_outputs(outputs: Iterable[str | None]) -> ShadowMetrics:
    """Measure schema-shaped outputs without making a network request."""
    total = valid = empty = parse_errors = 0
    for raw in outputs:
        total += 1
        if not str(raw or "").strip():
            empty += 1
            continue
        try:
            decoded = json.loads(str(raw))
            if not isinstance(decoded, dict):
                raise ValueError("response_not_object")
            required = {"intencion", "respuesta_bot", "datos_extraidos"}
            if not required.issubset(decoded) or not isinstance(decoded["datos_extraidos"], dict):
                raise ValueError("schema_fields_missing")
            valid += 1
        except (TypeError, ValueError, json.JSONDecodeError):
            parse_errors += 1
    return ShadowMetrics(total, valid, empty, parse_errors)
