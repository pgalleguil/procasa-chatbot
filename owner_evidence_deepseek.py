"""Strict DeepSeek evidence adjudicator shared by Yapo and TocToc.

The model never returns a probability or final classification. It can only
identify evidence codes backed by exact quotes; the deterministic probability
engine validates and weights those codes afterwards.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from typing import Any

import requests


PROMPT_VERSION = "owner-evidence-deepseek-v1"
ALLOWED_CODES = {
    "OWNER_FIRST_PERSON_EXPLICIT",
    "OWNER_FIRST_PERSON_POSSESSION",
    "OWNER_NO_COMMISSION_EXPLICIT",
    "SELLER_TYPE_OWNER",
    "SELLER_TYPE_PARTICULAR",
    "PERSONAL_IDENTITY_NO_COMMERCIAL",
    "EXPLICIT_COMMERCIAL_IDENTITY",
    "PROFESSIONAL_BADGE",
    "SELLER_TYPE_AGENT_OR_COMPANY",
    "COMMISSION_OR_BROKERAGE_FEES",
    "COMMERCIAL_DESCRIPTION",
    "COMMERCIAL_PROFILE_CORRELATION",
    "FOUR_TO_SEVEN_PROPERTIES_90D",
    "EIGHT_OR_MORE_PROPERTIES_90D",
}


@dataclass(slots=True)
class EvidenceResult:
    status: str
    evidence: list[dict[str, str]] = field(default_factory=list)
    neutral_observations: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    message_content: str = ""
    reasoning_content: str = ""
    error: str = ""
    attempts: int = 0
    prompt_version: str = PROMPT_VERSION


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text.lower()).strip()


def _sanitize(value: Any, limit: int = 6000) -> str:
    text = str(value or "")
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", " ", text)
    text = re.sub(r"(?:(?:\+?56)?\s*)?(?:\(?\d{1,2}\)?\s*)?(?:\d[\s-]*){7,11}", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _quote_in_source(quote: str, source: str) -> bool:
    normalized_quote = _norm(quote)
    normalized_source = _norm(source)
    if normalized_quote and normalized_quote in normalized_source:
        return True
    # Some historic HTML already contains Unicode replacement characters.
    # Permit a single-character encoding difference in a sufficiently long
    # literal quote, but never semantic/paraphrased matching.
    compact_quote = re.sub(r"[^a-z0-9]", "", normalized_quote)
    compact_source = re.sub(r"[^a-z0-9]", "", normalized_source)
    if len(compact_quote) < 8 or not compact_source:
        return False
    width = len(compact_quote)
    for size in range(max(1, width - 1), min(len(compact_source), width + 1) + 1):
        for start in range(0, len(compact_source) - size + 1):
            if SequenceMatcher(None, compact_quote, compact_source[start:start + size]).ratio() >= 0.92:
                return True
    return False


def _source_payload(extracted: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": _sanitize(extracted.get("title"), 800),
        "description": _sanitize(extracted.get("description") or extracted.get("descripcion"), 6000),
        "publicador_visible": _sanitize(extracted.get("publicador_visible"), 300),
        "seller_name": _sanitize(extracted.get("seller_name"), 300),
        "contact_name": _sanitize(extracted.get("contact_name"), 300),
        "seller_type": _sanitize(extracted.get("seller_type"), 100),
        "seller_is_pro": bool(extracted.get("seller_is_pro")),
        "contact_badges_text": _sanitize(extracted.get("contact_badges_text"), 300),
        "company_name": _sanitize(extracted.get("company_name"), 300),
        "broker_brand": _sanitize(extracted.get("broker_brand"), 300),
        "listing_advertiser": _sanitize(extracted.get("listing_advertiser"), 300),
        "publisher_activity": extracted.get("publisher_activity") or {},
    }


def _messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    allowed = ", ".join(sorted(ALLOWED_CODES))
    system = f"""Analiza evidencia sobre quién publicó un aviso inmobiliario chileno.
NO calcules porcentajes, scores, confianza ni estado final.
Devuelve exclusivamente JSON con este esquema:
{{"evidence":[{{"code":"CODIGO_PERMITIDO","source_field":"campo","quote":"cita textual exacta","explanation":"motivo breve"}}],"neutral_observations":["observación"]}}
Códigos permitidos: {allowed}.
Cada evidencia debe contener una cita textual que exista literalmente en el campo indicado. No inventes datos.
Las referencias en tercera persona como "propiedad de un solo dueño", "vendida por sus dueños", "trato directo con el propietario" y referencias jurídicas son NEUTRALES.
Solo declaraciones inequívocas del anunciante en primera persona prueban identidad de dueño.
La ausencia de señales comerciales no prueba que sea dueño.
La cantidad de publicaciones nunca prueba corredor por sí sola.
Si no existe evidencia útil devuelve evidence=[] y explica lo neutral en neutral_observations.
"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _validate_evidence(parsed: dict[str, Any], payload: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    valid: list[dict[str, str]] = []
    rejected: list[str] = []
    for item in parsed.get("evidence") or []:
        if not isinstance(item, dict):
            rejected.append("not_object")
            continue
        code = str(item.get("code") or "").strip().upper()
        source_field = str(item.get("source_field") or "").strip()
        quote = str(item.get("quote") or "").strip()
        explanation = str(item.get("explanation") or "").strip()
        if code not in ALLOWED_CODES:
            rejected.append(f"invalid_code:{code}")
            continue
        source_value = payload.get(source_field)
        if isinstance(source_value, (dict, list)):
            source_text = json.dumps(source_value, ensure_ascii=False)
        else:
            source_text = str(source_value or "")
        if not quote or not _quote_in_source(quote, source_text):
            rejected.append(f"quote_not_found:{code}:{source_field}")
            continue
        valid.append({
            "code": code,
            "source_field": source_field,
            "quote": quote[:300],
            "explanation": explanation[:300],
        })
    return valid, rejected


def adjudicate_owner_evidence(
    extracted: dict[str, Any],
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout: int = 20,
    max_tokens: int = 900,
    max_attempts: int = 2,
) -> EvidenceResult:
    if not api_key:
        return EvidenceResult(status="DISABLED", error="missing_api_key")
    payload = _source_payload(extracted)
    body = {
        "model": model,
        "messages": _messages(payload),
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=body,
                timeout=timeout,
            )
            response.raise_for_status()
            raw = response.json()
            message = (raw.get("choices") or [{}])[0].get("message") or {}
            content = str(message.get("content") or "").strip()
            reasoning = str(message.get("reasoning_content") or "")
            if not content:
                last_error = "empty_content"
                raise ValueError(last_error)
            parsed = json.loads(content)
            if not isinstance(parsed, dict) or not isinstance(parsed.get("evidence", []), list):
                last_error = "invalid_schema"
                raise ValueError(last_error)
            valid, rejected = _validate_evidence(parsed, payload)
            neutral = [str(value)[:300] for value in parsed.get("neutral_observations") or [] if str(value).strip()]
            return EvidenceResult(
                status="VALID",
                evidence=valid,
                neutral_observations=neutral,
                raw=raw,
                payload=body,
                message_content=content,
                reasoning_content=reasoning,
                error=";".join(rejected),
                attempts=attempt,
            )
        except requests.Timeout:
            last_error = "timeout"
        except requests.RequestException as exc:
            last_error = f"request_error:{type(exc).__name__}"
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)[:200]
        if attempt < max_attempts:
            time.sleep(1.5 * attempt)
    return EvidenceResult(status="ERROR", payload=body, error=last_error, attempts=max_attempts)
