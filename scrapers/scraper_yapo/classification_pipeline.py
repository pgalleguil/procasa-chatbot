"""Cost-controlled, deterministic-first classification routing for Yapo ads.

The DeepSeek execution path is intentionally simulation-only in this version.
The cache helpers are provided for an explicitly authorized future pilot, but
this module never sends network requests or returns an AI owner confirmation.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .classifier_rules import classify_yapo_listing, normalize_text, load_rule_sets
    from .config import DATA_DIR
except ImportError:  # direct script execution
    from classifier_rules import classify_yapo_listing, normalize_text, load_rule_sets
    from config import DATA_DIR

CLASSIFIER_VERSION = "yapo-deterministic-v6"
PROMPT_VERSION = "deepseek-route-simulation-v1"
UNKNOWN_CANDIDATES_PATH = DATA_DIR / "unknown_broker_candidates.json"
DEEPSEEK_CACHE_PATH = DATA_DIR / "deepseek_classification_cache.jsonl"


def _listing_id(extracted: dict[str, Any]) -> str:
    return str(extracted.get("listing_id") or extracted.get("id") or extracted.get("url") or "unknown")


def content_fingerprint(extracted: dict[str, Any]) -> str:
    """Hash seller identity and complete text; descriptions are never clipped."""
    content = {
        key: extracted.get(key, "")
        for key in (
            "title", "description", "descripcion", "publicador_visible", "contact_name",
            "contact_logo_alt", "seller_jsonld_name", "listing_advertiser", "seller_type",
            "contact_badges_text", "publisher_profile_context",
        )
    }
    payload = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_key(extracted: dict[str, Any]) -> str:
    material = "|".join((_listing_id(extracted), content_fingerprint(extracted), CLASSIFIER_VERSION, PROMPT_VERSION))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def estimate_deepseek_cost_usd(extracted: dict[str, Any], *, max_output_tokens: int = 500) -> float:
    """Conservative cache-miss estimate using 4 chars/token and Flash peak rates."""
    text_chars = sum(len(str(extracted.get(key) or "")) for key in ("title", "description", "descripcion"))
    input_tokens = max(1, min(6000, text_chars) // 4)
    return (input_tokens * 0.30 + max(0, int(max_output_tokens)) * 1.20) / 1_000_000


def get_cached_deepseek_result(extracted: dict[str, Any], path: Path = DEEPSEEK_CACHE_PATH) -> dict[str, Any] | None:
    key = cache_key(extracted)
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("cache_key") == key:
                    return record
    except FileNotFoundError:
        pass
    return None


def store_deepseek_result(
    extracted: dict[str, Any],
    result: dict[str, Any],
    *,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float,
    path: Path = DEEPSEEK_CACHE_PATH,
) -> None:
    """Persist a prior authorized adjudication; never invoked by simulation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "cache_key": cache_key(extracted),
        "listing_id": _listing_id(extracted),
        "content_sha256": content_fingerprint(extracted),
        "classifier_version": CLASSIFIER_VERSION,
        "prompt_version": PROMPT_VERSION,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "result": result,
        "input_tokens": max(0, int(input_tokens)),
        "output_tokens": max(0, int(output_tokens)),
        "estimated_cost_usd": max(0.0, float(estimated_cost_usd)),
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _atomic_json_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def record_unknown_business_candidate(
    extracted: dict[str, Any],
    classification: dict[str, Any],
    path: Path = UNKNOWN_CANDIDATES_PATH,
) -> dict[str, Any] | None:
    contextual = classification.get("contextual_analysis") or {}
    evidence = contextual.get("unknown_business_identity_evidence") or []
    if not evidence:
        return None
    publisher_fields = (
        "publicador_visible", "contact_name", "contact_logo_alt", "listing_advertiser",
        "seller_jsonld_name",
    )
    name = next((str(extracted.get(field) or "").strip() for field in publisher_fields if extracted.get(field)), "")
    if not name:
        return None
    row = {
        "normalized_name": normalize_text(name),
        "display_name": name,
        "listing_id": _listing_id(extracted),
        "url": str(extracted.get("url") or ""),
        "content_sha256": content_fingerprint(extracted),
        "evidence": list(evidence),
        "status": "pending_manual_verification",
        "first_detected_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        rows = []
    identity = (row["normalized_name"], row["listing_id"], row["content_sha256"])
    if any((item.get("normalized_name"), str(item.get("listing_id")), item.get("content_sha256")) == identity for item in rows):
        return next(item for item in rows if (item.get("normalized_name"), str(item.get("listing_id")), item.get("content_sha256")) == identity)
    rows.append(row)
    _atomic_json_write(path, rows)
    return row


def promote_verified_business_candidate(
    normalized_name: str,
    *,
    listing_id: str,
    verified: bool,
    verification_source: str,
    registry_path: Path = UNKNOWN_CANDIDATES_PATH,
    catalog_path: Path | None = None,
) -> str:
    """Promote a registry entry only after an explicit verified=True decision."""
    if not verified:
        raise ValueError("La identidad no puede promocionarse sin verificación explícita")
    if not verification_source.strip():
        raise ValueError("verification_source es obligatorio para conservar la trazabilidad")
    try:
        candidates = json.loads(registry_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        raise ValueError("No existe el registro de candidatas o su JSON no es válido")
    target_norm = normalize_text(normalized_name)
    row = next((item for item in candidates if item.get("normalized_name") == target_norm and str(item.get("listing_id")) == str(listing_id)), None)
    if row is None:
        raise ValueError("No se encontró la identidad candidata para ese anuncio")
    path = catalog_path or (DATA_DIR / "known_broker_brands.json")
    catalog = json.loads(path.read_text(encoding="utf-8-sig"))
    brands = list(catalog.get("brands") or [])
    already_known = any(normalize_text(name) == target_norm for name in brands)
    if not already_known:
        brands.append(row["display_name"])
        catalog["brands"] = sorted(set(brands), key=lambda item: normalize_text(item))
        aliases = catalog.setdefault("brand_aliases", {})
        aliases.setdefault(row["display_name"], [])
        metadata = catalog.setdefault("catalog_metadata", {})
        confirmed = metadata.setdefault("manually_verified_additions", [])
        confirmed.append({
            "name": row["display_name"],
            "listing_id": str(listing_id),
            "verification_source": verification_source.strip(),
            "verified_at": datetime.now(timezone.utc).isoformat(),
        })
        _atomic_json_write(path, catalog)
    row["status"] = "confirmed_broker"
    row["verification_source"] = verification_source.strip()
    row["confirmed_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_json_write(registry_path, candidates)
    load_rule_sets.cache_clear()
    try:
        from . import classifier_rules
    except ImportError:
        import classifier_rules
    classifier_rules._known_brand_compact_index.cache_clear()
    return "already_known" if already_known else "added_to_catalog"


def classify_with_routing(
    extracted: dict[str, Any],
    *,
    simulate_deepseek: bool = True,
    max_calls: int = 0,
    max_budget_usd: float = 0.0,
    budget_used_usd: float = 0.0,
    deepseek_calls_used: int = 0,
    cache_path: Path | None = None,
    candidate_registry_path: Path | None = None,
) -> dict[str, Any] | None:
    """Return deterministic classification plus a no-cost proposed IA route."""
    result = classify_yapo_listing(extracted)
    if result is None:
        return None
    state = result.get("state")
    route = "deterministic_only"
    reason = ""
    if state == "AD_REMOVED":
        route, reason = "removed", "Anuncio eliminado; sin clasificación comercial."
    elif state in {"CORREDOR_SEGURO", "CORREDOR_PROBABLE"}:
        route, reason = "deterministic_only", "La evidencia comercial resuelve el caso sin IA."
    else:
        route = "deepseek_candidate"
        reason = "Caso ambiguo o propietario probable; se simula el enrutamiento sin llamada real."
    cached = get_cached_deepseek_result(extracted, path=cache_path or DEEPSEEK_CACHE_PATH)
    estimated_cost = 0.0 if cached is not None else estimate_deepseek_cost_usd(extracted)
    if route == "deepseek_candidate" and cached is not None:
        route = "deepseek_cached"
        reason = "Hay una adjudicación en caché para el mismo anuncio, contenido y versiones; no se repite una llamada."
    budget_exhausted = bool(
        route == "deepseek_candidate"
        and (max_calls <= 0 or deepseek_calls_used >= max_calls
             or max_budget_usd <= 0 or budget_used_usd + estimated_cost > max_budget_usd)
    )
    result = {
        **result,
        "classification_route": route,
        "deepseek_simulation_only": bool(simulate_deepseek and route == "deepseek_candidate"),
        "deepseek_call_executed": False,
        "deepseek_cache_hit": cached is not None,
        "cached_deepseek_result": (cached or {}).get("result") if cached else None,
        "deepseek_budget_max_calls": max(0, int(max_calls)),
        "deepseek_calls_used": max(0, int(deepseek_calls_used)),
        "deepseek_budget_max_usd": max(0.0, float(max_budget_usd)),
        "deepseek_budget_used_usd": max(0.0, float(budget_used_usd)),
        "deepseek_estimated_cost_usd": estimated_cost,
        "deepseek_budget_exhausted": budget_exhausted,
        "deepseek_route_reason": reason,
        "content_sha256": content_fingerprint(extracted),
        "classifier_version": CLASSIFIER_VERSION,
    }
    if candidate_registry_path is not None:
        record_unknown_business_candidate(extracted, result, path=candidate_registry_path)
    return result
