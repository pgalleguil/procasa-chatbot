"""DeepSeek classifier with proper status handling: empty content, reasoning_content, fallback."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

try:
    import requests
except Exception:
    requests = None

from classifier_rules import load_rule_sets, normalize_text
try:
    from config import AppConfig
except ImportError:  # The core CRM config is already imported in an orchestrated run.
    from scrapers.scraper_toctoc.config import AppConfig

try:
    from broker_identity import detect_hard_broker_signal
except ImportError:  # ejecución directa desde scrapers/scraper_toctoc/
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from broker_identity import detect_hard_broker_signal

try:
    from ai_entrypoint import is_authorized as _is_authorized_ai_entrypoint
except ImportError:  # ejecución directa desde scrapers/scraper_toctoc/
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ai_entrypoint import is_authorized as _is_authorized_ai_entrypoint


class DeepSeekStatus(str, Enum):
    NOT_NEEDED = "NOT_NEEDED"
    VALID = "VALID"
    TRUNCATED = "TRUNCATED"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    INVALID_EMPTY_CONTENT = "INVALID_EMPTY_CONTENT"
    INVALID_JSON = "INVALID_JSON"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    INVALID_STATE = "INVALID_STATE"
    API_ERROR = "API_ERROR"
    TIMEOUT = "TIMEOUT"


VALID_STATES = {"CORREDOR_SEGURO", "CORREDOR_PROBABLE", "DUEÑO_SEGURO", "DUENO_SEGURO", "INCIERTO", "AD_REMOVED"}


@dataclass(slots=True)
class DeepSeekResult:
    state: str
    confidence: float
    reason: str
    evidence: list[str]
    raw: dict[str, Any]
    status: str = DeepSeekStatus.VALID.value
    rule_fallback_state: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    message_content: str = ""
    reasoning_content: str = ""
    structured_evidence: list[dict[str, str]] = field(default_factory=list)
    prompt_version: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DeepSeekCall:
    status: str
    data: dict[str, Any] | None
    content: str
    finish_reason: str
    http_status: int | None = None
    response_body_length: int | None = None
    transport_error: str = ""
    raw_response_body: str = ""
    raw_content: str = ""
    reasoning_content: str = ""


_CONTRACT_CLASSIFICATIONS = {"OWNER", "BROKER", "UNCERTAIN"}
_CONTRACT_TO_LEGACY_STATE = {
    # Preserve the existing four-state classifier without overstating a broad
    # BROKER response: it maps to the existing probable state, not a hard veto.
    "OWNER": "DUEÑO_SEGURO",
    "BROKER": "CORREDOR_PROBABLE",
    "UNCERTAIN": "INCIERTO",
}


def _balanced_json_objects(text: str) -> list[tuple[str, int, int]]:
    """Return balanced top-level object candidates without interpreting prose.

    Objects nested inside a JSON array are not treated as top-level response
    objects. Braces inside quoted strings and escaped quotes are ignored.
    """
    found: list[tuple[str, int, int]] = []
    square_depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "[":
            square_depth += 1
            index += 1
            continue
        if char == "]":
            square_depth = max(0, square_depth - 1)
            index += 1
            continue
        if char != "{" or square_depth:
            index += 1
            continue

        start = index
        depth = 0
        in_string = False
        escaped = False
        while index < len(text):
            current = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
            elif current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    found.append((text[start:end], start, end))
                    index = end
                    break
            index += 1
    return found


def _extract_json_object(content: str) -> tuple[dict[str, Any] | None, str, str]:
    """Extract one unambiguous JSON object from plain, fenced, or wrapped text.

    Returns (object, extraction_mode, error_kind). Empty content is reported
    separately; this function never substitutes reasoning text or a default
    classification for missing answer content.
    """
    if not isinstance(content, str) or not content.strip():
        return None, "empty", "empty"
    text = content.strip().lstrip("\ufeff").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    else:
        if isinstance(parsed, dict):
            return parsed, "plain_json", ""
        return None, "plain_json_non_object", "schema"

    candidates = _balanced_json_objects(text)
    parsed_candidates: list[tuple[dict[str, Any], int, int]] = []
    non_object_json_found = False
    for candidate, start, end in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            parsed_candidates.append((value, start, end))
        else:
            non_object_json_found = True

    if len(parsed_candidates) > 1:
        return None, "ambiguous_json_objects", "ambiguous"
    if len(parsed_candidates) == 1:
        parsed, start, end = parsed_candidates[0]
        prefix = text[:start]
        suffix = text[end:]
        fenced = "```" in prefix or "```" in suffix
        return parsed, "markdown_fenced_json" if fenced else "embedded_json", ""
    if non_object_json_found:
        return None, "embedded_json_non_object", "schema"
    return None, "unparseable", "json"


def _normalize_response_object(
    parsed: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Validate the current legacy response and the approved tri-state shape."""
    has_legacy_state = "state" in parsed
    has_contract_classification = "classification" in parsed
    if has_legacy_state == has_contract_classification:
        return None, "response must contain exactly one of state/classification"

    confidence = parsed.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None, "confidence must be a number"
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None, "confidence must be between 0 and 1"

    if has_contract_classification:
        classification = parsed.get("classification")
        evidence = parsed.get("evidence")
        if not isinstance(classification, str) or classification not in _CONTRACT_CLASSIFICATIONS:
            return None, "classification is not an allowed value"
        if not isinstance(evidence, str):
            return None, "evidence must be a string"
        return {
            "state": _CONTRACT_TO_LEGACY_STATE[classification],
            "confidence": confidence,
            "reason": evidence,
            "signals": [evidence] if evidence.strip() else [],
        }, ""

    state = parsed.get("state")
    if not isinstance(state, str) or state.strip() not in VALID_STATES:
        return None, "state is not an allowed value"
    reason = parsed.get("reason", "")
    if not isinstance(reason, str):
        return None, "reason must be a string"
    evidence = parsed.get("evidence", parsed.get("signals", []))
    if isinstance(evidence, str):
        evidence = [evidence] if evidence.strip() else []
    if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
        return None, "evidence/signals must be a string or list of strings"
    return {
        "state": state.strip(),
        "confidence": confidence,
        "reason": reason,
        "signals": evidence,
    }, ""


_EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?:(?:\+?56)?\s*)?(?:\(?\d{1,2}\)?\s*)?(?:\d[\s-]*){7,11}")


def _clean_text(text: str) -> str:
    return _PHONE_RE.sub(" ", _EMAIL_RE.sub(" ", re.sub(r"\s+", " ", (text or "").strip()))).strip()


def _dedupe_join(parts: list[str]) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for part in parts:
        piece = re.sub(r"\s+", " ", part).strip()
        if not piece: continue
        key = normalize_text(piece)
        if not key or key in seen: continue
        seen.add(key); ordered.append(piece)
    return "\n\n".join(ordered)


def _signal_windows(text: str, radius: int, terms: list[str]) -> list[str]:
    lowered = text.lower(); windows = []
    for term in terms:
        needle = term.lower().strip()
        if not needle: continue
        start = 0
        while True:
            idx = lowered.find(needle, start)
            if idx < 0: break
            windows.append(text[max(0, idx - radius):min(len(text), idx + len(needle) + radius)].strip())
            start = idx + len(needle)
    return windows


def _description_signal_terms() -> list[str]:
    terms: list[str] = []
    for key in ("known_broker_brands", "hard_broker_terms", "company_shape_terms", "owner_keywords"):
        terms.extend(load_rule_sets().get(key, []))
    deduped = []; seen = set()
    for t in terms:
        n = normalize_text(str(t))
        if n and n not in seen: seen.add(n); deduped.append(str(t))
    return deduped


def build_description_for_llm(text: str, max_chars: int = 6000, *, head_chars: int = 2500, tail_chars: int = 2500, snippet_radius: int = 350, signal_terms: list[str] | None = None) -> dict[str, Any]:
    original = _clean_text(text or ""); original_len = len(original)
    signals = signal_terms or _description_signal_terms()
    if original_len <= max_chars:
        return {"text_for_llm": original, "original_len": original_len, "sent_len": original_len, "truncated_for_llm": False, "strategy": "full_text"}
    head = original[:head_chars].strip(); tail = original[-tail_chars:].strip() if tail_chars > 0 else ""
    snippets = _signal_windows(original, snippet_radius, signals)
    combined = _dedupe_join(["[INICIO DESCRIPCION]", head, "[FRAGMENTOS CON SENALES]", *snippets, "[FINAL DESCRIPCION]", tail])
    if len(combined) > max_chars:
        combined = _dedupe_join(["[INICIO DESCRIPCION]", head, "[FRAGMENTOS CON SENALES]", "[FINAL DESCRIPCION]", tail])[:max_chars].rstrip()
    return {"text_for_llm": combined, "original_len": original_len, "sent_len": len(combined), "truncated_for_llm": True, "strategy": "head_tail_signal_snippets"}


def _build_messages(extracted: dict[str, Any], rule_context: dict[str, Any], desc_bundle: dict[str, Any]) -> list[dict[str, str]]:
    payload = {k: (extracted.get(k) if isinstance(extracted.get(k), (str, int, float, bool, list)) else str(extracted.get(k, ""))) for k in ("title", "publicador_visible", "contact_name", "contact_logo_alt", "listing_advertiser", "seller_jsonld_name", "contact_badges_text", "company_name", "broker_brand", "seller_is_pro", "seller_profile_id", "seller_profile_url", "seller_profile_logo", "operation_label_raw")}
    payload.update({k: rule_context.get(k) for k in ("company_like_suspected", "company_like_evidence", "known_brand_evidence", "hard_broker_evidence", "owner_signal_evidence", "weak_broker_evidence")})
    payload["seller_type"] = extracted.get("seller_type", "DESCONOCIDO")
    payload["seller_type_source"] = extracted.get("seller_type_source", "")
    payload["seller_type_evidence"] = extracted.get("seller_type_evidence", "")
    payload["dormitorios"] = extracted.get("dormitorios", "")
    payload["banos"] = extracted.get("banos", "")
    payload["superficie"] = extracted.get("superficie_total", "")
    payload["descripcion_para_llm"] = desc_bundle["text_for_llm"]
    payload["llm_description_original_len"] = desc_bundle["original_len"]
    payload["llm_description_sent_len"] = desc_bundle["sent_len"]
    payload["llm_description_truncated"] = desc_bundle["truncated_for_llm"]
    payload["llm_description_strategy"] = desc_bundle["strategy"]
    system = (
        "Eres un clasificador de anuncios inmobiliarios chilenos. "
        "Clasifica el publicador del anuncio en uno de estos estados: "
        "CORREDOR_SEGURO, CORREDOR_PROBABLE, DUEÑO_SEGURO, INCIERTO. "
        "Usa estas reglas:\n"
        "1. CORREDOR_SEGURO: Evidencia clara de corredora, inmobiliaria, agente profesional.\n"
        "2. CORREDOR_PROBABLE: Senales de actividad comercial sin confirmacion clara.\n"
        "3. DUEÑO_SEGURO: Requiere EVIDENCIA POSITIVA EXPLICITA de que el anunciante es el propietario. Ejemplos validos: "
        "\"vendo mi casa\", \"arriendo mi departamento\", \"soy dueno\", \"soy propietario\". "
         "Solo sirven declaraciones inequívocas EN PRIMERA PERSONA. Referencias como "
         "\"propiedad de un solo dueno\", \"documentacion de los propietarios\", "
         "\"trato directo con dueno\" o \"vendida por sus duenos\" NO identifican a quien publica.\n"
         "4. INCIERTO: No hay evidencia suficiente para determinar. Es el estado por defecto.\n"
         "REGLAS ESTRICTAS:\n"
         "- seller_type='PARTICULAR' NUNCA es suficiente por si solo para DUEÑO_SEGURO.\n"
         "- \"vende directamente\", \"venta directa\" o \"arriendo directo\" SIN mencion explicita del dueno NO son suficientes.\n"
         "- Solo la primera persona posesiva ligada a propiedad (por ejemplo, \"vendo mi casa\") demuestra propiedad; invitaciones a contactar o visitar son neutrales.\n"
         "- Frases como \"contáctame\", \"escríbeme\", \"agenda una visita\", \"coordinar visita\" son NEUTRALES.\n"
         "- La ausencia de una empresa NO implica dueno. Busca siempre evidencia positiva.\n"
         "- Si no hay evidencia positiva clara, responde INCIERTO.\n"
        "Responde SOLO en JSON compacto con este formato exacto:\n"
        '{"state": "INCIERTO", "confidence": 0.5, "reason": "No hay evidencia suficiente.", "signals": []}\n'
        "Los estados permitidos son: CORREDOR_SEGURO, CORREDOR_PROBABLE, DUEÑO_SEGURO, INCIERTO."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def _call_deepseek(body: dict, config: AppConfig, headers: dict):
    """Make a single DeepSeek API call.
    Returns a response envelope for both success and failure."""
    try:
        resp = requests.post(
            f"{config.deepseek_base_url.rstrip('/')}/chat/completions",
            headers=headers, json=body, timeout=config.deepseek_timeout_seconds)
    except requests.exceptions.Timeout as exc:
        failed_response = getattr(exc, "response", None)
        failed_body = str(getattr(failed_response, "text", "") or "")
        return DeepSeekCall(
            DeepSeekStatus.TIMEOUT.value, None, "", "",
            http_status=getattr(failed_response, "status_code", None),
            response_body_length=len(failed_body.encode("utf-8")),
            transport_error=type(exc).__name__,
            raw_response_body=failed_body,
        )
    except requests.exceptions.RequestException as e:
        failed_response = getattr(e, "response", None)
        failed_body = str(getattr(failed_response, "text", "") or "")
        return DeepSeekCall(
            DeepSeekStatus.API_ERROR.value, None, "", "",
            http_status=getattr(failed_response, "status_code", None),
            response_body_length=len(failed_body.encode("utf-8")),
            transport_error=type(e).__name__,
            raw_response_body=failed_body,
        )
    http_status = getattr(resp, "status_code", None)
    response_body = str(getattr(resp, "text", "") or "")
    response_body_length = len(response_body.encode("utf-8"))
    try:
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return DeepSeekCall(
            DeepSeekStatus.API_ERROR.value, None, "", "",
            http_status=http_status,
            response_body_length=response_body_length,
            raw_response_body=response_body,
        )
    if not isinstance(data, dict):
        return DeepSeekCall(
            DeepSeekStatus.API_ERROR.value, None, "", "",
            http_status=http_status,
            response_body_length=response_body_length,
            raw_response_body=response_body,
        )
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    raw_content = message.get("content")
    raw_content = raw_content if isinstance(raw_content, str) else ""
    content = raw_content.strip()
    finish_reason = str(choice.get("finish_reason") or "")
    reasoning_content = message.get("reasoning_content")
    reasoning_content = reasoning_content if isinstance(reasoning_content, str) else ""
    return DeepSeekCall(
        DeepSeekStatus.VALID.value, data, content, finish_reason,
        http_status=http_status,
        response_body_length=response_body_length,
        raw_response_body=response_body,
        raw_content=raw_content,
        reasoning_content=reasoning_content,
    )


def _response_diagnostics(
    call: DeepSeekCall,
    body: dict[str, Any],
    *,
    attempt: int,
    parse_mode: str = "",
) -> dict[str, Any]:
    data = call.data or {}
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    reasoning = message.get("reasoning_content")
    reasoning = reasoning if isinstance(reasoning, str) else ""
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {
        "http_status": call.http_status,
        "finish_reason": call.finish_reason,
        "content_length_chars": len(call.raw_content),
        "content_length_bytes": len(call.raw_content.encode("utf-8")),
        "reasoning_content_present": bool(call.reasoning_content or reasoning),
        "reasoning_content_length_chars": len(call.reasoning_content or reasoning),
        "usage": dict(usage),
        "response_model": str(data.get("model") or ""),
        "requested_model": str(body.get("model") or ""),
        "max_tokens": body.get("max_tokens"),
        "response_format": body.get("response_format"),
        "temperature": body.get("temperature"),
        "response_body_length_bytes": call.response_body_length,
        "transport_error": call.transport_error,
        "attempt": attempt,
        "parse_mode": parse_mode,
    }


def _persist_attempt_result(
    ledger: Any,
    attempt_row: dict[str, Any] | None,
    call: DeepSeekCall,
    body: dict[str, Any],
    *,
    parser_status: str,
    parser_error: str = "",
    normalized: dict[str, Any] | None = None,
    api_key: str = "",
) -> None:
    if ledger is None or attempt_row is None:
        return
    data = call.data or {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    normalized = normalized or {}
    secret = str(api_key or "")
    redact = lambda value: value.replace(secret, "[REDACTED]") if secret else value
    ledger.finish_attempt(attempt_row, {
        "status": parser_status,
        "model_returned": str(data.get("model") or ""),
        "http_status": call.http_status,
        "finish_reason": call.finish_reason,
        "max_tokens_requested": body.get("max_tokens"),
        "max_tokens_effective": body.get("max_tokens"),
        "response_format_effective": body.get("response_format"),
        "temperature_effective": body.get("temperature"),
        "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
        "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
        "total_tokens": usage.get("total_tokens"),
        # Keep the provider's raw answer, except for an unlikely echoed
        # credential. The original character/byte lengths remain available in
        # the length fields below for diagnosis.
        "raw_content": redact(call.raw_content),
        "raw_reasoning_content": redact(call.reasoning_content),
        "raw_response_body": redact(call.raw_response_body),
        "content_length": len(call.raw_content),
        "parser_status": parser_status,
        "parser_error": str(parser_error or ""),
        "classification": str(normalized.get("state") or ""),
        "confidence": normalized.get("confidence"),
        "evidence": list(normalized.get("signals") or []),
        "reason": str(normalized.get("reason") or ""),
        "transport_error": call.transport_error,
        "response_body_length_bytes": call.response_body_length,
    })


def classify_with_deepseek(
    extracted: dict[str, Any],
    rule_context: dict[str, Any],
    config: AppConfig,
    desc_bundle: dict[str, Any] | None = None,
    *,
    authorization_token: object | None = None,
    call_ledger: Any | None = None,
    call_context: dict[str, Any] | None = None,
    on_additional_attempt: Callable[[int], bool] | None = None,
    on_attempt_complete: Callable[[dict[str, Any]], None] | None = None,
) -> DeepSeekResult | None:
    # This low-level function is deliberately fail-closed. Productive
    # captacion calls must originate in classification_service, which supplies
    # the private authorization token after cache, health and budget gates.
    if not _is_authorized_ai_entrypoint(authorization_token):
        return DeepSeekResult(
            state="INCIERTO",
            confidence=0.5,
            reason="Unauthorized DeepSeek entry point; use classification_service.",
            evidence=[],
            raw={},
            status=DeepSeekStatus.NOT_NEEDED.value,
        )
    hard_publisher = detect_hard_broker_signal(extracted=extracted)
    if hard_publisher:
        return DeepSeekResult(
            state="CORREDOR_SEGURO",
            confidence=1.0,
            reason="HARD VETO: publicador/perfil comercial explícito; DeepSeek no fue invocado.",
            evidence=[hard_publisher["evidence"]],
            raw={},
            status=DeepSeekStatus.NOT_NEEDED.value,
            structured_evidence=[],
            prompt_version="hard_publisher_broker_veto",
        )
    if not config.deepseek_enabled or not config.deepseek_api_key or requests is None:
        return None
    if "pro" in config.deepseek_model.lower():
        raise RuntimeError("Modelo DeepSeek Pro no permitido.")

    if desc_bundle is None:
        desc_bundle = build_description_for_llm(
            extracted.get("descripcion", extracted.get("description", "")),
            max_chars=config.deepseek_description_max_chars)
    
    headers = {"Authorization": f"Bearer {config.deepseek_api_key}", "Content-Type": "application/json"}
    default_tokens = max(1, int(getattr(
        config,
        "ai_output_token_budget_default",
        getattr(config, "deepseek_max_tokens", 500),
    ) or 500))
    retry_tokens = max(default_tokens, int(getattr(
        config,
        "ai_output_token_budget_retry",
        max(default_tokens, 800),
    ) or default_tokens))
    body = {
        "model": config.deepseek_model,
        "messages": _build_messages(extracted, rule_context, desc_bundle),
        "max_tokens": default_tokens,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        # This classifier uses raw HTTP requests (not the OpenAI SDK), so the
        # provider parameter belongs at the top level, not under extra_body.
        "thinking": {"type": "disabled"},
    }

    configured_attempts = int(getattr(
        config,
        "ai_max_attempts_per_fingerprint",
        getattr(config, "deepseek_max_attempts", 2),
    ) or 1)
    max_attempts = max(1, min(2, configured_attempts))
    call = DeepSeekCall(DeepSeekStatus.API_ERROR.value, None, "", "")
    data = None
    content = ""
    parsed = None
    parse_mode = ""
    last_error_kind = ""
    normalized: dict[str, Any] | None = None
    attempt_summaries: list[dict[str, Any]] = []

    def diagnostics_for(last_call: DeepSeekCall, last_body: dict[str, Any], attempt_number: int, mode: str = "") -> dict[str, Any]:
        diagnostics = _response_diagnostics(last_call, last_body, attempt=attempt_number, parse_mode=mode)
        diagnostics["attempts"] = list(attempt_summaries)
        return diagnostics

    for attempt in range(max_attempts):
        if attempt:
            body["max_tokens"] = retry_tokens
        attempt_row = (
            call_ledger.begin_attempt(dict(call_context or {}), body)
            if call_ledger is not None else None
        )
        call = _call_deepseek(body, config, headers)
        data, content = call.data, call.content
        usage = data.get("usage") if isinstance(data, dict) and isinstance(data.get("usage"), dict) else {}
        attempt_summary = {
            "attempt_number": int((attempt_row or {}).get("attempt_number") or attempt + 1),
            "http_status": call.http_status,
            "finish_reason": call.finish_reason,
            "max_tokens": body.get("max_tokens"),
            "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
            "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
            "total_tokens": usage.get("total_tokens"),
            "model_returned": str((data or {}).get("model") or ""),
            "max_tokens": body.get("max_tokens"),
        }
        attempt_summaries.append(attempt_summary)

        if call.status != DeepSeekStatus.VALID.value:
            _persist_attempt_result(
                call_ledger, attempt_row, call, body,
                parser_status=call.status,
                parser_error=call.transport_error or call.status,
                api_key=config.deepseek_api_key,
            )
            if on_attempt_complete:
                on_attempt_complete(attempt_summary)
            return DeepSeekResult(
                state="INCIERTO", confidence=0.5,
                reason=f"DeepSeek {call.status.lower()}; retryable technical failure",
                evidence=[], raw=data or {}, status=call.status,
                diagnostics=diagnostics_for(
                    call, body,
                    int((attempt_row or {}).get("attempt_number") or attempt + 1),
                ),
            )

        # A provider length stop is always a technical truncation, even if the
        # partial text happens to contain a parseable JSON prefix. It must not
        # silently become a final classification.
        if call.finish_reason == "length":
            _persist_attempt_result(
                call_ledger, attempt_row, call, body,
                parser_status="TRUNCATED",
                parser_error="finish_reason=length",
                api_key=config.deepseek_api_key,
            )
            if on_attempt_complete:
                on_attempt_complete(attempt_summary)
            if attempt + 1 < max_attempts:
                if on_additional_attempt and not on_additional_attempt(retry_tokens):
                    return DeepSeekResult(
                        state="INCIERTO", confidence=0.5,
                        reason="AI_BUDGET_EXCEEDED before truncation retry",
                        evidence=[], raw=data or {}, status="AI_BUDGET_EXCEEDED",
                        message_content=call.raw_content,
                        reasoning_content=call.reasoning_content,
                        diagnostics=diagnostics_for(
                            call, body,
                            int((attempt_row or {}).get("attempt_number") or attempt + 1),
                        ),
                    )
                continue
            return DeepSeekResult(
                state="INCIERTO", confidence=0.5,
                reason="DeepSeek response truncated after bounded retry; remains retryable",
                evidence=[], raw=data or {}, status=DeepSeekStatus.TRUNCATED.value,
                message_content=call.raw_content,
                reasoning_content=call.reasoning_content,
                diagnostics=diagnostics_for(
                    call, body,
                    int((attempt_row or {}).get("attempt_number") or attempt + 1),
                ),
            )

        if not content:
            _persist_attempt_result(
                call_ledger, attempt_row, call, body,
                parser_status="EMPTY",
                parser_error="provider returned empty message content",
                api_key=config.deepseek_api_key,
            )
            if on_attempt_complete:
                on_attempt_complete(attempt_summary)
            return DeepSeekResult(
                state="INCIERTO", confidence=0.5,
                reason="DeepSeek returned empty content",
                evidence=[], raw=data or {}, status=DeepSeekStatus.INVALID_EMPTY_CONTENT.value,
                message_content=call.raw_content,
                reasoning_content=call.reasoning_content,
                diagnostics=diagnostics_for(
                    call, body,
                    int((attempt_row or {}).get("attempt_number") or attempt + 1),
                ),
            )

        parsed, parse_mode, last_error_kind = _extract_json_object(content)
        if not isinstance(parsed, dict):
            parser_status = (
                "SCHEMA_ERROR" if last_error_kind in {"schema", "ambiguous"}
                else "INVALID_JSON"
            )
            _persist_attempt_result(
                call_ledger, attempt_row, call, body,
                parser_status=parser_status,
                parser_error=f"{parse_mode}:{last_error_kind or 'invalid_json'}",
                api_key=config.deepseek_api_key,
            )
            if on_attempt_complete:
                on_attempt_complete(attempt_summary)
            public_status = (
                DeepSeekStatus.INVALID_SCHEMA.value
                if parser_status == "SCHEMA_ERROR"
                else DeepSeekStatus.INVALID_JSON.value
            )
            return DeepSeekResult(
                state="INCIERTO", confidence=0.5,
                reason=f"DeepSeek response could not be parsed ({parse_mode})",
                evidence=[], raw=data or {}, status=public_status,
                message_content=call.raw_content,
                reasoning_content=call.reasoning_content,
                diagnostics=diagnostics_for(
                    call, body,
                    int((attempt_row or {}).get("attempt_number") or attempt + 1),
                    parse_mode,
                ),
            )

        normalized, schema_error = _normalize_response_object(parsed)
        if normalized is None:
            invalid_status = (
                DeepSeekStatus.INVALID_STATE.value
                if "state is not an allowed value" in schema_error
                else DeepSeekStatus.INVALID_SCHEMA.value
            )
            _persist_attempt_result(
                call_ledger, attempt_row, call, body,
                parser_status="SCHEMA_ERROR",
                parser_error=schema_error,
                api_key=config.deepseek_api_key,
            )
            if on_attempt_complete:
                on_attempt_complete(attempt_summary)
            return DeepSeekResult(
                state="INCIERTO", confidence=0.5,
                reason=f"DeepSeek invalid response schema: {schema_error}",
                evidence=[], raw=data or {}, status=invalid_status,
                message_content=call.raw_content,
                reasoning_content=call.reasoning_content,
                diagnostics=diagnostics_for(
                    call, body,
                    int((attempt_row or {}).get("attempt_number") or attempt + 1),
                    parse_mode,
                ),
            )

        _persist_attempt_result(
            call_ledger, attempt_row, call, body,
            parser_status="VALID",
            normalized=normalized,
            api_key=config.deepseek_api_key,
        )
        if on_attempt_complete:
            on_attempt_complete(attempt_summary)
        break

    if not isinstance(parsed, dict) or normalized is None:
        return DeepSeekResult(
            state="INCIERTO", confidence=0.5,
            reason="DeepSeek response was not a valid structured object",
            evidence=[], raw=data or {}, status=DeepSeekStatus.INVALID_SCHEMA.value,
            diagnostics=diagnostics_for(
                call, body,
                int((attempt_row or {}).get("attempt_number") or attempt + 1),
                parse_mode,
            ),
        )
    
    raw_state = normalized["state"]
    if raw_state == "DUENO_SEGURO":
        raw_state = "DUEÑO_SEGURO"
    evidence = [str(e) for e in normalized["signals"] if e.strip()]
    diagnostics = diagnostics_for(
        call, body,
        int((attempt_row or {}).get("attempt_number") or attempt + 1),
        parse_mode,
    )
    
    return DeepSeekResult(
        state=raw_state, confidence=normalized["confidence"],
        reason=normalized["reason"], evidence=evidence,
        raw=data, status=DeepSeekStatus.VALID.value, payload=body,
        message_content=call.raw_content,
        reasoning_content=call.reasoning_content,
        diagnostics=diagnostics,
    )
