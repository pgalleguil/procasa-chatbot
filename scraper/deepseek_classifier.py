from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore

from classifier_rules import load_rule_sets, normalize_text
from config import AppConfig


@dataclass(slots=True)
class DeepSeekResult:
    state: str
    confidence: float
    reason: str
    evidence: list[str]
    raw: dict[str, Any]
    payload: dict[str, Any]
    message_content: str
    reasoning_content: str
    status: str = "VALID"
    structured_evidence: list[dict[str, str]] = field(default_factory=list)
    prompt_version: str = ""


_EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?:(?:\+?56)?\s*)?(?:\(?\d{1,2}\)?\s*)?(?:\d[\s-]*){7,11}")


def sanitize_description_for_llm(text: str, max_chars: int = 3500) -> str:
    bundle = build_description_for_llm(text, max_chars=max_chars)
    return bundle["text_for_llm"]


def _clean_text(text: str) -> str:
    cleaned = _EMAIL_RE.sub(" ", text)
    cleaned = _PHONE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _dedupe_join(parts: list[str]) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for part in parts:
        piece = re.sub(r"\s+", " ", part).strip()
        if not piece:
            continue
        key = normalize_text(piece)
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(piece)
    return "\n\n".join(ordered)


def _signal_windows(text: str, radius: int, terms: list[str]) -> list[str]:
    lowered = text.lower()
    windows: list[str] = []
    for term in terms:
        needle = term.lower().strip()
        if not needle:
            continue
        start = 0
        while True:
            idx = lowered.find(needle, start)
            if idx < 0:
                break
            left = max(0, idx - radius)
            right = min(len(text), idx + len(needle) + radius)
            windows.append(text[left:right].strip())
            start = idx + len(needle)
    return windows


def _description_signal_terms() -> list[str]:
    rule_sets = load_rule_sets()
    terms: list[str] = []
    for key in ("known_broker_brands", "hard_broker_terms", "company_shape_terms", "owner_keywords"):
        terms.extend(rule_sets.get(key, []))
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        norm = normalize_text(str(term))
        if norm and norm not in seen:
            seen.add(norm)
            deduped.append(str(term))
    return deduped


def build_description_for_llm(
    text: str,
    max_chars: int = 6000,
    *,
    head_chars: int = 2500,
    tail_chars: int = 2500,
    snippet_radius: int = 350,
    signal_terms: list[str] | None = None,
) -> dict[str, Any]:
    original = _clean_text(text or "")
    original_len = len(original)
    signals = signal_terms if signal_terms is not None else _description_signal_terms()

    if original_len <= max_chars:
        return {
            "text_for_llm": original,
            "original_len": original_len,
            "sent_len": original_len,
            "truncated_for_llm": False,
            "strategy": "full_text",
        }

    head = original[:head_chars].strip()
    tail = original[-tail_chars:].strip() if tail_chars > 0 else ""
    snippets = _signal_windows(original, snippet_radius, signals)

    combined = _dedupe_join(
        [
            "[INICIO DESCRIPCION]",
            head,
            "[FRAGMENTOS CON SENALES]",
            *snippets,
            "[FINAL DESCRIPCION]",
            tail,
        ]
    )

    if len(combined) > max_chars:
        base = _dedupe_join(["[INICIO DESCRIPCION]", head, "[FRAGMENTOS CON SENALES]", "[FINAL DESCRIPCION]", tail])
        combined = base if len(base) <= max_chars else base[:max_chars].rstrip()

    return {
        "text_for_llm": combined,
        "original_len": original_len,
        "sent_len": len(combined),
        "truncated_for_llm": True,
        "strategy": "head_tail_signal_snippets",
    }


def _build_messages(extracted: dict[str, Any], rule_context: dict[str, Any], description_bundle: dict[str, Any]) -> list[dict[str, str]]:
    payload = {
        "publicador_visible": extracted.get("publicador_visible", ""),
        "contact_name": extracted.get("contact_name", ""),
        "contact_logo_alt": extracted.get("contact_logo_alt", ""),
        "seller_type": extracted.get("seller_type", ""),
        "listing_advertiser": extracted.get("listing_advertiser", ""),
        "seller_jsonld_name": extracted.get("seller_jsonld_name", ""),
        "contact_badges_text": extracted.get("contact_badges_text", ""),
        "company_like_suspected": rule_context.get("company_like_suspected", False),
        "company_like_evidence": rule_context.get("company_like_evidence", []),
        "known_brand_evidence": rule_context.get("known_brand_evidence", []),
        "hard_broker_evidence": rule_context.get("hard_broker_evidence", []),
        "owner_signal_evidence": rule_context.get("owner_signal_evidence", []),
        "weak_broker_evidence": rule_context.get("weak_broker_evidence", []),
        "publisher_profile_context": rule_context.get("publisher_profile_context", {}),
        "descripcion_para_llm": description_bundle["text_for_llm"],
        "llm_description_original_len": description_bundle["original_len"],
        "llm_description_sent_len": description_bundle["sent_len"],
        "llm_description_truncated": description_bundle["truncated_for_llm"],
        "llm_description_strategy": description_bundle["strategy"],
    }
    system = (
        "Clasifica anuncios inmobiliarios de Yapo como CORREDOR_SEGURO, CORREDOR_PROBABLE, DUEÑO_SEGURO o INCIERTO. "
        "Analiza especialmente el nombre del publicador y las pistas comerciales del anuncio. "
        "No clasifiques como dueño solo por ausencia de una empresa en el JSON. "
        "Si el publicador parece un actor comercial o intermediario, trátalo como señal fuerte. "
        "Las pistas débiles de visita o contacto solo valen en combinación con otras evidencias. "
        "Solo una declaración inequívoca en primera persona (por ejemplo 'soy dueño' o 'vendo mi casa') prueba dueño. "
        "Frases en tercera persona como 'trato directo con sus dueños' o 'vendida por sus dueños' son débiles y no identifican al publicador. "
        "La descripción puede estar resumida con inicio, final y fragmentos relevantes. Presta atención a esas zonas. "
        "No asumas que falta de evidencia en el extracto equivale a dueño. "
        "Responde en JSON compacto con keys state, confidence, reason, evidence."
    )
    user = json.dumps(payload, ensure_ascii=False)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def classify_with_deepseek(
    extracted: dict[str, Any],
    rule_context: dict[str, Any],
    config: AppConfig,
    description_bundle: dict[str, Any] | None = None,
) -> DeepSeekResult | None:
    if not config.deepseek_enabled:
        return None
    if "pro" in config.deepseek_model.lower():
        raise RuntimeError("Modelo DeepSeek Pro no permitido. Usar deepseek-v4-flash.")
    if not config.deepseek_api_key:
        return None
    if requests is None:
        raise RuntimeError("La libreria requests es requerida para DeepSeek.")

    # The model is an evidence extractor only.  State and percentage are
    # produced later by the shared deterministic engine in mongo_store.
    try:
        from owner_evidence_deepseek import adjudicate_owner_evidence
    except ImportError:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from owner_evidence_deepseek import adjudicate_owner_evidence
    evidence_result = adjudicate_owner_evidence(
        extracted,
        api_key=config.deepseek_api_key,
        base_url=config.deepseek_base_url,
        model=config.deepseek_model,
        timeout=config.deepseek_timeout_seconds,
        max_tokens=max(config.deepseek_max_tokens, 900),
        max_attempts=2,
    )
    return DeepSeekResult(
        state="INCIERTO",
        confidence=0.0,
        reason="DeepSeek extrajo evidencia estructurada; la decisión final es determinística.",
        evidence=[item.get("quote", "") for item in evidence_result.evidence],
        raw=evidence_result.raw,
        payload=evidence_result.payload,
        message_content=evidence_result.message_content,
        reasoning_content=evidence_result.reasoning_content,
        status=evidence_result.status,
        structured_evidence=evidence_result.evidence,
        prompt_version=evidence_result.prompt_version,
    )

    if description_bundle is None:  # pragma: no cover - legacy implementation retained for rollback
        description_bundle = build_description_for_llm(
            extracted.get("descripcion", extracted.get("description", "")),
            max_chars=config.deepseek_description_max_chars,
            head_chars=config.deepseek_description_head_chars,
            tail_chars=config.deepseek_description_tail_chars,
            snippet_radius=config.deepseek_description_snippet_radius,
        )

    base_url = config.deepseek_base_url.rstrip("/")
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": config.deepseek_model,
        "messages": _build_messages(extracted, rule_context, description_bundle),
        "max_tokens": config.deepseek_max_tokens,
        "temperature": 0,
    }
    if config.deepseek_thinking:
        body["thinking"] = True

    response = requests.post(url, headers=headers, json=body, timeout=config.deepseek_timeout_seconds)
    response.raise_for_status()
    data = response.json()
    message = data.get("choices", [{}])[0].get("message", {})
    content = message.get("content", "").strip()
    reasoning_content = str(message.get("reasoning_content") or "")
    if not content:
        return None
    status = "VALID"
    try:
        parsed = json.loads(content)
    except Exception:
        parsed = {"state": "INCIERTO", "confidence": 0.5, "reason": content, "evidence": []}
        status = "INVALID_JSON"

    state = str(parsed.get("state", "INCIERTO"))
    if state not in {"CORREDOR_SEGURO", "CORREDOR_PROBABLE", "DUEÑO_SEGURO", "DUENO_SEGURO", "INCIERTO", "AD_REMOVED"}:
        state = "INCIERTO"
        status = "INVALID_STATE"

    return DeepSeekResult(
        state=state,
        confidence=float(parsed.get("confidence", 0.5)),
        reason=str(parsed.get("reason", "")),
        evidence=[str(item) for item in parsed.get("evidence", []) if str(item).strip()],
        raw=data,
        payload=body,
        message_content=content,
        reasoning_content=reasoning_content,
        status=status,
    )
