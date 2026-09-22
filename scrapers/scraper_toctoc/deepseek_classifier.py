"""DeepSeek classifier with proper status handling: empty content, reasoning_content, fallback."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

try:
    import requests
except Exception:
    requests = None

from classifier_rules import load_rule_sets, normalize_text
from config import AppConfig

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
    INVALID_EMPTY_CONTENT = "INVALID_EMPTY_CONTENT"
    INVALID_JSON = "INVALID_JSON"
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
    Returns (status, data, content, finish_reason) on both success and failure."""
    import time as _time
    try:
        resp = requests.post(
            f"{config.deepseek_base_url.rstrip('/')}/chat/completions",
            headers=headers, json=body, timeout=config.deepseek_timeout_seconds)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        return DeepSeekStatus.TIMEOUT.value, None, None, None
    except requests.exceptions.RequestException as e:
        return DeepSeekStatus.API_ERROR.value, None, None, None
    try:
        data = resp.json()
    except Exception:
        return DeepSeekStatus.API_ERROR.value, None, None, None
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    finish_reason = data.get("choices", [{}])[0].get("finish_reason", "")
    return DeepSeekStatus.VALID.value, data, content, finish_reason


def classify_with_deepseek(
    extracted: dict[str, Any],
    rule_context: dict[str, Any],
    config: AppConfig,
    desc_bundle: dict[str, Any] | None = None,
    *,
    authorization_token: object | None = None,
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
    import time as _time
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
    body = {
        "model": config.deepseek_model,
        "messages": _build_messages(extracted, rule_context, desc_bundle),
        "max_tokens": config.deepseek_max_tokens,
        "temperature": 0,
    }
    # Disable thinking/reasoning to avoid empty content (DeepSeek V4 format)
    body["extra_body"] = {"thinking": {"type": "disabled"}}
    # Force JSON output mode
    body["response_format"] = {"type": "json_object"}
    
    max_attempts = max(1, int(getattr(config, "deepseek_max_attempts", 2) or 1))
    status = DeepSeekStatus.API_ERROR.value
    data = None
    content = ""
    finish_reason = ""
    parsed = None
    for attempt in range(max_attempts):
        status, data, content, finish_reason = _call_deepseek(body, config, headers)
        if status != DeepSeekStatus.VALID.value:
            if attempt + 1 < max_attempts:
                _time.sleep(2)
                continue
            return DeepSeekResult(
                state="INCIERTO", confidence=0.5,
                reason=f"DeepSeek {status.lower()} after {attempt + 1} attempt(s)",
                evidence=[], raw={}, status=status)
        if not content:
            if attempt + 1 < max_attempts:
                _time.sleep(2)
                continue
            return DeepSeekResult(
                state="INCIERTO", confidence=0.5,
                reason="DeepSeek returned empty content",
                evidence=[], raw=data or {}, status=DeepSeekStatus.INVALID_EMPTY_CONTENT.value)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            break
        if attempt + 1 < max_attempts and finish_reason == "length" and config.deepseek_max_tokens < 800:
            body["max_tokens"] = 800
            continue
        return DeepSeekResult(
            state="INCIERTO", confidence=0.5,
            reason=f"DeepSeek invalid JSON: {content[:200]}",
            evidence=[], raw=data or {}, status=DeepSeekStatus.INVALID_JSON.value)
    
    if not isinstance(parsed, dict):
        return DeepSeekResult(
            state="INCIERTO", confidence=0.5, reason="DeepSeek response not a dict",
            evidence=[], raw=data, status=DeepSeekStatus.INVALID_JSON.value)
    
    # Validate state
    raw_state = str(parsed.get("state", "")).strip()
    if raw_state not in VALID_STATES:
        return DeepSeekResult(
            state="INCIERTO", confidence=0.5,
            reason=f"DeepSeek invalid state: {raw_state}",
            evidence=[], raw=data, status=DeepSeekStatus.INVALID_STATE.value)
    
    # Normalize DUEÑO vs DUENO
    if raw_state == "DUENO_SEGURO":
        raw_state = "DUEÑO_SEGURO"
    
    # Parse confidence
    confidence = 0.5
    try:
        confidence = float(parsed.get("confidence", 0.5))
    except (ValueError, TypeError):
        confidence = 0.5
    
    evidence = [str(e) for e in (parsed.get("evidence", []) or parsed.get("signals", [])) if str(e).strip()]
    
    return DeepSeekResult(
        state=raw_state, confidence=confidence,
        reason=str(parsed.get("reason", "")), evidence=evidence,
        raw=data, status=DeepSeekStatus.VALID.value, payload=body,
        message_content=content,
        reasoning_content=str((data or {}).get("choices", [{}])[0].get("message", {}).get("reasoning_content") or ""))
