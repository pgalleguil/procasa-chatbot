#!/usr/bin/env python
"""Controlled read-only reprocess of owner-probability contradictions and S/I."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatbot.storage import get_db
from config import Config
from owner_evidence_deepseek import EvidenceResult, adjudicate_owner_evidence
from owner_probability import calculate_owner_probability, expected_state_for_probability, normalize_state
from dry_run_owner_probability_active import (
    _trim_extracted,
    build_profile_activity,
    enrich_from_local_html,
    load_extractors,
)


CONCLUSIVE_CODES = {
    "OWNER_FIRST_PERSON_EXPLICIT",
    "EXPLICIT_COMMERCIAL_IDENTITY",
    "PROFESSIONAL_BADGE",
    "SELLER_TYPE_AGENT_OR_COMPANY",
    "COMMISSION_OR_BROKERAGE_FEES",
}


def _json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _fetch_live(doc: dict[str, Any], extractor) -> tuple[dict[str, Any], str]:
    url = str(doc.get("url") or doc.get("canonical_url") or "")
    if not url:
        return {}, "NO_URL"
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
                "Accept-Language": "es-CL,es;q=0.9",
            },
            timeout=20,
        )
        if response.status_code in {404, 410}:
            return {}, f"REMOVED_HTTP_{response.status_code}"
        response.raise_for_status()
        html = response.text
        low = html.lower()
        removed = any(value in low for value in (
            "anuncio borrado", "eliminado por el anunciante", "propiedad no encontrada",
            "esta propiedad ya no se encuentra disponible", "publicacion expiro",
        ))
        if removed:
            return {}, "REMOVED_CONTENT"
        return _trim_extracted(extractor(html, url)), "LIVE_REEXTRACTED"
    except Exception as exc:
        return {}, f"LIVE_ERROR:{type(exc).__name__}"


def _probe(doc: dict[str, Any], extracted: dict[str, Any], now: datetime) -> dict[str, Any]:
    overlay = dict(extracted)
    overlay["deepseek_structured_evidence_status"] = "VALID"
    overlay["deepseek_structured_evidence"] = []
    return calculate_owner_probability(doc, extracted=overlay, calculated_at=now)


def _needs_deepseek(probe: dict[str, Any]) -> bool:
    if not probe["owner_probability_completeness"]["complete"]:
        return False
    codes = {item["code"] for item in probe["owner_probability_signals"]["applied"]}
    return not bool(codes & CONCLUSIVE_CODES)


def _strong_evidence_conflict(signals: list[dict[str, Any]]) -> bool:
    positive = [item for item in signals if item["weight"] >= 25]
    negative = [item for item in signals if item["weight"] <= -25]
    return bool(positive and negative)


def _ds_config() -> dict[str, Any]:
    return {
        "api_key": os.getenv("DEEPSEEK_API_KEY", ""),
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model": os.getenv("DEEPSEEK_ADJUDICATOR_MODEL", os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")),
        "timeout": int(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "20")),
        "max_tokens": max(900, int(os.getenv("DEEPSEEK_MAX_TOKENS", "900"))),
        "max_attempts": 2,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-report",
        default=str(ROOT / "reports" / "owner_probability_active_dry_run_20260714_235826.json"),
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-deepseek", type=int, default=500)
    parser.add_argument("--reuse-traces", default="")
    parser.add_argument("--output-dir", default=str(ROOT / "reports"))
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    source = json.loads(Path(args.source_report).read_text(encoding="utf-8"))
    old_rows = source["rows"]
    target_keys = {
        (row["origen"], str(row["listing_id"]))
        for row in old_rows if row["contradiccion"] or not row["completo"]
    }
    old_by_key = {(row["origen"], str(row["listing_id"])): row for row in old_rows}
    collection = Config.get_captacion_collection(get_db())
    docs = list(collection.find({
        "$or": [
            {"origen": origin, "listing_id": listing_id}
            for origin, listing_id in sorted(target_keys)
        ]
    }))
    extractors = load_extractors()

    reused_results: dict[tuple[str, str], EvidenceResult] = {}
    if args.reuse_traces:
        with Path(args.reuse_traces).open("r", encoding="utf-8") as handle:
            for line in handle:
                item = json.loads(line)
                if item.get("status") != "VALID":
                    continue
                key = (str(item.get("origen")), str(item.get("listing_id")))
                reused_results[key] = EvidenceResult(
                    status="VALID", evidence=item.get("evidence") or [],
                    neutral_observations=item.get("neutral_observations") or [],
                    raw=item.get("raw") or {}, payload=item.get("payload") or {},
                    message_content=str(item.get("message_content") or ""),
                    reasoning_content=str(item.get("reasoning_content") or ""),
                    error=str(item.get("error") or ""), attempts=int(item.get("attempts") or 0),
                    prompt_version=str(item.get("prompt_version") or "owner-evidence-deepseek-v1"),
                )

    enriched_docs = []
    live_attempts = Counter()
    for index, doc in enumerate(docs, 1):
        origin = str(doc.get("origen") or "").lower()
        extracted, html_status, html_path = enrich_from_local_html(doc, extractors[origin])
        extracted = _trim_extracted(extracted)
        probe = _probe(doc, extracted, now)
        extraction_reasons = [
            reason for reason in probe["owner_probability_completeness"]["reasons"]
            if not reason.startswith("DEEPSEEK_")
        ]
        live_status = "NOT_NEEDED"
        if extraction_reasons or html_status != "REEXTRACTED":
            live, live_status = _fetch_live(doc, extractors[origin])
            live_attempts[live_status.split(":", 1)[0]] += 1
            if live:
                extracted.update(live)
        enriched_docs.append((doc, extracted, html_status, html_path, live_status))
        if index % 100 == 0:
            print(f"Reextraídos {index}/{len(docs)}")

    activity_input = [(doc, extracted, status, path) for doc, extracted, status, path, _ in enriched_docs]
    activity = build_profile_activity(collection, activity_input, now)
    planned = []
    preliminary = {}
    for doc, extracted, html_status, html_path, live_status in enriched_docs:
        origin = str(doc.get("origen") or "").lower()
        profile_id = str(extracted.get("seller_profile_id") or doc.get("seller_profile_id") or "").strip()
        if profile_id and (origin, profile_id) in activity:
            extracted["publisher_activity"] = activity[(origin, profile_id)]
        probe = _probe(doc, extracted, now)
        key = (origin, str(doc.get("listing_id")))
        preliminary[key] = probe
        existing_structured = (doc.get("classification") or {}).get("deepseek_structured_evidence")
        existing_status = (doc.get("classification") or {}).get("deepseek_structured_evidence_status")
        removed_confirmed = str(live_status).startswith("REMOVED_")
        if removed_confirmed:
            pass
        elif key in reused_results:
            extracted["deepseek_structured_evidence_status"] = "VALID"
            extracted["deepseek_structured_evidence"] = reused_results[key].evidence
        elif existing_status == "VALID" and isinstance(existing_structured, list):
            extracted["deepseek_structured_evidence_status"] = "VALID"
            extracted["deepseek_structured_evidence"] = existing_structured
        elif _needs_deepseek(probe):
            planned.append((key, extracted))

    planned_by_origin = Counter(key[0] for key, _ in planned)
    over_limit = {origin: count for origin, count in planned_by_origin.items() if count > args.max_deepseek}
    if over_limit:
        raise RuntimeError(
            f"Llamadas DeepSeek por scraper exceden límite {args.max_deepseek}: {over_limit}"
        )

    cfg = _ds_config()
    print(f"DeepSeek planificadas: {len(planned)}; por origen: {dict(planned_by_origin)}")
    ds_results: dict[tuple[str, str], EvidenceResult] = dict(reused_results)
    lock = Lock()
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        future_map = {
            pool.submit(adjudicate_owner_evidence, extracted, **cfg): key
            for key, extracted in planned
        }
        for future in as_completed(future_map):
            key = future_map[future]
            try:
                ds_results[key] = future.result()
            except Exception as exc:
                ds_results[key] = EvidenceResult(status="ERROR", error=f"worker:{type(exc).__name__}")
            completed += 1
            if completed % 25 == 0 or completed == len(planned):
                print(f"DeepSeek completadas {completed}/{len(planned)}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    trace_path = output_dir / f"owner_probability_targeted_deepseek_traces_{stamp}.jsonl"
    with trace_path.open("w", encoding="utf-8") as handle:
        for key, result in sorted(ds_results.items()):
            handle.write(json.dumps({
                "origen": key[0], "listing_id": key[1], "status": result.status,
                "evidence": result.evidence, "neutral_observations": result.neutral_observations,
                "error": result.error, "attempts": result.attempts, "prompt_version": result.prompt_version,
                "message_content": result.message_content, "reasoning_content": result.reasoning_content,
                "raw": result.raw, "payload": result.payload,
            }, ensure_ascii=False, default=_json_default) + "\n")

    rows = []
    for doc, extracted, html_status, html_path, live_status in enriched_docs:
        key = (str(doc.get("origen") or "").lower(), str(doc.get("listing_id")))
        result = ds_results.get(key)
        if result:
            extracted["deepseek_structured_evidence_status"] = result.status
            extracted["deepseek_structured_evidence"] = result.evidence if result.status == "VALID" else None
        elif "deepseek_structured_evidence_status" not in extracted:
            extracted["deepseek_structured_evidence_status"] = "VALID"
            extracted["deepseek_structured_evidence"] = []
        removed_confirmed = str(live_status).startswith("REMOVED_")
        probability = calculate_owner_probability(doc, extracted=extracted, calculated_at=now)
        signals = probability["owner_probability_signals"]["applied"]
        value = probability["owner_probability"]
        if removed_confirmed:
            value = None
            proposed_state = "AD_REMOVED"
        else:
            proposed_state = expected_state_for_probability(value) if value is not None else "PENDIENTE"
        old = old_by_key[key]
        evidence_conflict = _strong_evidence_conflict(signals)
        needs_manual = (value is None and not removed_confirmed) or evidence_conflict
        gestion = doc.get("gestion") or {}
        assigned = bool(gestion.get("ejecutivo_asignado") or gestion.get("ejecutivo_id"))
        rows.append({
            "origen": key[0], "listing_id": key[1], "url": doc.get("url"),
            "estado_anterior": normalize_state((doc.get("classification") or {}).get("state")),
            "estado_propuesto": proposed_state,
            "owner_probability": value,
            "porcentaje": None if value is None else round(value * 100),
            "banda": probability["owner_probability_band"],
            "completo": probability["owner_probability_completeness"]["complete"],
            "motivos_pendiente": (["REMOVED_CONFIRMED"] if removed_confirmed else probability["owner_probability_completeness"]["reasons"]),
            "senales": signals,
            "evidence_conflict": evidence_conflict,
            "revision_humana": needs_manual,
            "era_contradiccion": bool(old["contradiccion"]),
            "era_si": not old["completo"],
            "contradiccion_resuelta": bool(old["contradiccion"]) and not needs_manual,
            "si_recuperado": (not old["completo"]) and (value is not None or removed_confirmed) and not evidence_conflict,
            "removed_confirmed": removed_confirmed,
            "asignada": assigned,
            "ejecutivo": gestion.get("ejecutivo_asignado") or gestion.get("ejecutivo_nombre") or "",
            "asignada_pasa_corredor": assigned and value is not None and value < 0.5,
            "html_status": html_status, "live_status": live_status,
            "deepseek_status": result.status if result else "NOT_NEEDED_RULE_FINAL",
            "deepseek_attempts": result.attempts if result else 0,
            "deepseek_error": result.error if result else "",
            "previous_classification_state": normalize_state((doc.get("classification") or {}).get("state")),
            "transition_reason": "owner_probability_band_v1",
        })

    bands = Counter(row["banda"] for row in rows)
    proposed = Counter(row["estado_propuesto"] for row in rows)
    ds_statuses = Counter(row["deepseek_status"] for row in rows)
    summary = {
        "executed_at": now.isoformat(), "read_only": True,
        "target_expected": len(target_keys), "target_found": len(rows),
        "contradictions_input": sum(row["era_contradiccion"] for row in rows),
        "contradictions_resolved": sum(row["contradiccion_resuelta"] for row in rows),
        "si_input": sum(row["era_si"] for row in rows),
        "si_recovered": sum(row["si_recuperado"] for row in rows),
        "human_review": sum(row["revision_humana"] for row in rows),
        "still_si": sum(row["owner_probability"] is None and not row["removed_confirmed"] for row in rows),
        "removed_confirmed": sum(row["removed_confirmed"] for row in rows),
        "assigned_input": sum(row["asignada"] for row in rows),
        "assigned_to_broker": sum(row["asignada_pasa_corredor"] for row in rows),
        "bands": dict(bands), "proposed_states": dict(proposed),
        "deepseek_planned": len(planned), "deepseek_statuses": dict(ds_statuses),
        "deepseek_planned_by_origin": dict(planned_by_origin),
        "deepseek_reused_valid": len(reused_results),
        "live_attempts": dict(live_attempts),
        "incomplete_fifty": sum(not row["completo"] and row["porcentaje"] == 50 for row in rows),
        "incierto_outside_band": sum(
            row["estado_propuesto"] == "INCIERTO" and not (50 <= row["porcentaje"] <= 69)
            for row in rows if row["porcentaje"] is not None
        ),
        "strong_evidence_conflicts": sum(row["evidence_conflict"] for row in rows),
    }
    report_path = output_dir / f"owner_probability_targeted_dry_run_{stamp}.json"
    report_path.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    assigned_path = output_dir / f"owner_probability_assigned_to_broker_{stamp}.json"
    assigned_path.write_text(json.dumps([row for row in rows if row["asignada_pasa_corredor"]], ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    print(json.dumps({"summary": summary, "report": str(report_path), "traces": str(trace_path), "assigned": str(assigned_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
