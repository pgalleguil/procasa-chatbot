"""Back up, dry-run, apply, and roll back the owner-score migration.

Dry-run is the default and never writes to MongoDB. The migration cohort is
frozen by the legacy ``classification.owner_probability`` field populated in
the prior 1,776-document operation.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import json_util
from pymongo import MongoClient, UpdateOne

from config import Config
from owner_scoring import (
    build_source_signal_snapshot,
    calculate_owner_score,
    compute_publisher_activity,
    propose_classification_state,
    publisher_identity_key,
)


ROOT = Path(__file__).resolve().parent
COHORT_QUERY = {"classification.owner_probability": {"$exists": True}}
RULE_VERSION = "classification-owner-score-v1"
BACKUP_FIELDS = (
    "classification", "gestion", "source_signals", "owner_score",
    "owner_score_version", "owner_score_signals",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_production_yapo_parser():
    """Load the exact parser version used by the production Yapo scraper."""
    path = ROOT / "scraping" / "scraping_yapo_proxys.py"
    if not path.exists():
        raise RuntimeError(f"Versioned Yapo parser missing: {path}")
    sys.path[:0] = [str(ROOT), str(path.parent)]
    spec = importlib.util.spec_from_file_location("production_yapo_parser", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _first(*values):
    return next((value for value in values if value not in (None, "", "N/A", "S/I")), "")


def resolve_historical_html(raw_path: Any) -> Path | None:
    path = Path(str(raw_path or ""))
    candidates = [
        path,
        ROOT / "scraping" / path,
        Path(r"C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok - copia (2)\scraping") / path,
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def normalize_state(value: Any) -> str:
    text = str(value or "SIN_ESTADO")
    if text.startswith("DUE") and text.endswith("O_SEGURO"):
        return "DUE\u00d1O_SEGURO"
    return text


def canonical_signal_input(doc: dict[str, Any], yapo_parser=None) -> dict[str, Any]:
    """Map stored portal fields to the shared scorer without inventing signals."""
    details = doc.get("details") if isinstance(doc.get("details"), dict) else {}
    data = {**details, **doc}

    # Historical Yapo is re-read only through the production parser itself.
    html_path = resolve_historical_html(doc.get("html_path"))
    if doc.get("origen") in {"yapo", "yapo.cl"} and yapo_parser and html_path:
        parsed = yapo_parser._parse_html_fast(
            html_path.read_text(encoding="utf-8", errors="replace")
        ) or {}
        data.update({key: value for key, value in parsed.items() if value not in (None, "", "N/A")})

    stored_sources = doc.get("source_signals") if isinstance(doc.get("source_signals"), dict) else {}
    data["description"] = _first(data.get("description"), data.get("descripcion"))
    data["publicador_visible"] = _first(
        data.get("publicador_visible"), data.get("publicador"), data.get("seller_name"),
        stored_sources.get("publisher_visible"),
    )
    data["publicador"] = data["publicador_visible"]
    data["company_name"] = _first(data.get("company_name"), stored_sources.get("company_name"))
    data["broker_brand"] = _first(data.get("broker_brand"), stored_sources.get("broker_brand"))
    data["seller_type"] = _first(data.get("seller_type"), stored_sources.get("seller_type"))
    data["seller_is_pro"] = bool(
        data.get("seller_is_pro") or stored_sources.get("seller_is_pro")
    )
    data["seller_profile_id"] = _first(
        data.get("seller_profile_id"), stored_sources.get("publisher_profile_id")
    )
    classification = doc.get("classification") if isinstance(doc.get("classification"), dict) else {}
    data["classifier_original_signals"] = {
        "signals": classification.get("signals") or {},
        "evidence": classification.get("evidence") or [],
        "decision_source": classification.get("decision_source") or classification.get("source") or "",
        "reason": classification.get("reason") or "",
    }
    return data


def _assignment_snapshot(gestion: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "ejecutivo_asignado", "ejecutivo_email", "ejecutivo_id", "ejecutivo_nombre",
        "fecha_asignacion", "asignacion_comuna_slug", "asignacion_version",
        "assignment_weight", "classification_at_assignment", "estado", "estado_captacion",
    )
    return {key: gestion.get(key) for key in keys if key in gestion}


def is_assigned(doc: dict[str, Any]) -> bool:
    gestion = doc.get("gestion") if isinstance(doc.get("gestion"), dict) else {}
    return bool(_first(
        gestion.get("ejecutivo_asignado"), gestion.get("ejecutivo_nombre"),
        gestion.get("ejecutivo_email"), gestion.get("ejecutivo_id"),
    ))


def build_plan(
    doc: dict[str, Any], data: dict[str, Any], result, proposed_state: str,
    calculated_at: datetime,
) -> dict[str, Any]:
    old_classification = deepcopy(doc.get("classification") or {})
    previous_state = normalize_state(old_classification.get("state"))
    source_snapshot = build_source_signal_snapshot(data)
    stable_unchanged = (
        old_classification.get("owner_score") == result.score
        and old_classification.get("owner_score_version") == result.version
        and old_classification.get("owner_score_signals") == list(result.signals)
        and old_classification.get("state") == proposed_state
        and doc.get("source_signals") == source_snapshot
    )
    score_time = old_classification.get("owner_score_calculated_at") if stable_unchanged else calculated_at
    new_classification = deepcopy(old_classification)
    new_classification.update({
        "state": proposed_state,
        "owner_score": result.score,
        "owner_score_version": result.version,
        "owner_score_signals": list(result.signals),
        "owner_score_calculated_at": score_time,
        "previous_classification_state": old_classification.get(
            "previous_classification_state", previous_state
        ),
        "classification_rule_version": RULE_VERSION,
        "owner_score_signal_origin": {
            "portal": doc.get("origen") or doc.get("source_portal") or "",
            "parser": "scraping.scraping_yapo_proxys" if doc.get("origen") in {"yapo", "yapo.cl"} else "stored_toctoc_fields",
            "snapshot": "source_signals",
        },
    })
    new_gestion = deepcopy(doc.get("gestion") or {})
    removed = proposed_state == "CORREDOR_SEGURO" and is_assigned(doc)
    if removed:
        previous_assignment = _assignment_snapshot(new_gestion)
        history = list(new_gestion.get("historial_retiros_clasificacion") or [])
        history.append({
            "at": calculated_at,
            "reason": "Reclasificada como CORREDOR_SEGURO por owner-score-v1",
            "previous_assignment": previous_assignment,
            "previous_state": previous_state,
        })
        new_gestion.update({
            "historial_retiros_clasificacion": history,
            "ejecutivo_asignado": None,
            "ejecutivo_email": None,
            "ejecutivo_id": None,
            "ejecutivo_nombre": None,
            "excluir_asignacion": True,
            "excluir_asignacion_reason": "CORREDOR_SEGURO",
        })
    return {
        "_id": doc["_id"],
        "listing_id": str(doc.get("listing_id") or doc["_id"]),
        "origin": doc.get("origen") or "",
        "previous_state": previous_state,
        "proposed_state": proposed_state,
        "technical_confidence": old_classification.get("confidence"),
        "owner_score": result.score,
        "signals": list(result.signals),
        "useful_signal_count": result.useful_signal_count,
        "assigned": is_assigned(doc),
        "remove_assignment": removed,
        "set": {
            "classification": new_classification,
            "gestion": new_gestion,
            "source_signals": source_snapshot,
        },
    }


def create_backup(collection, docs: list[dict[str, Any]], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "owner-score-full-backup-v1",
        "created_at": _now(),
        "database": Config.DB_NAME,
        "collection": Config.CAPTACION_COLLECTION_NAME,
        "query": COHORT_QUERY,
        "count": len(docs),
        "documents": docs,
    }
    raw = json_util.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    destination.write_bytes(raw)
    manifest = {
        "backup": str(destination),
        "count": len(docs),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "created_at": _now().isoformat(),
    }
    destination.with_suffix(destination.suffix + ".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return destination


def summarize(plans: list[dict[str, Any]]) -> dict[str, Any]:
    before = Counter(plan["previous_state"] for plan in plans)
    after = Counter(plan["proposed_state"] for plan in plans)
    scores = Counter(plan["owner_score"] for plan in plans)
    transitions = Counter(
        f'{plan["previous_state"]} -> {plan["proposed_state"]}' for plan in plans
        if plan["previous_state"] != plan["proposed_state"]
    )
    contradictions = []
    for plan in plans:
        incoherent = (
            plan["proposed_state"] == "DUEÑO_SEGURO" and plan["owner_score"] < 70
        ) or (
            plan["proposed_state"] == "CORREDOR_SEGURO" and plan["owner_score"] > 35
        )
        if incoherent:
            contradictions.append(plan["listing_id"])
    return {
        "mode": "DRY_RUN_READ_ONLY",
        "cohort_count": len(plans),
        "states_before": dict(sorted(before.items())),
        "states_after": dict(sorted(after.items())),
        "owner_score_distribution": {str(k): v for k, v in sorted(scores.items())},
        "state_changes": sum(transitions.values()),
        "transitions": dict(sorted(transitions.items())),
        "requested_transitions": {
            name: transitions.get(name, 0) for name in (
                "DUEÑO_SEGURO -> INCIERTO",
                "DUEÑO_SEGURO -> CORREDOR_SEGURO",
                "INCIERTO -> DUEÑO_SEGURO",
                "INCIERTO -> CORREDOR_SEGURO",
            )
        },
        "exactly_50": sum(plan["owner_score"] == 50 for plan in plans),
        "without_useful_signals": sum(plan["useful_signal_count"] == 0 for plan in plans),
        "contradictions_state_score": len(contradictions),
        "contradiction_listing_ids": contradictions,
        "would_remove_from_executives": sum(plan["remove_assignment"] for plan in plans),
        "proposed_brokers_currently_unassigned": sum(
            plan["proposed_state"] == "CORREDOR_SEGURO" and not plan["assigned"] for plan in plans
        ),
    }


def rollback_operations(backup_payload: dict[str, Any]) -> list[UpdateOne]:
    operations = []
    for doc in backup_payload["documents"]:
        restore = {field: deepcopy(doc.get(field)) for field in BACKUP_FIELDS}
        operations.append(UpdateOne({"_id": doc["_id"]}, {"$set": restore}))
    return operations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, help="Create a complete cohort backup")
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "owner_score_full_dry_run_20260714.json")
    parser.add_argument("--apply", action="store_true", help="Apply plans; requires --approved-backup")
    parser.add_argument("--approved-backup", type=Path)
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()

    collection = MongoClient(Config.MONGO_URI)[Config.DB_NAME][Config.CAPTACION_COLLECTION_NAME]
    if args.rollback:
        payload = json_util.loads(args.rollback.read_text(encoding="utf-8"))
        result = collection.bulk_write(rollback_operations(payload), ordered=False)
        print(json.dumps({"rollback_matched": result.matched_count, "modified": result.modified_count}))
        return 0

    docs = list(collection.find(COHORT_QUERY))
    if len(docs) != 1776:
        raise RuntimeError(f"Safety stop: expected frozen cohort of 1776, found {len(docs)}")
    if args.backup:
        create_backup(collection, docs, args.backup)

    yapo_parser = _load_production_yapo_parser()
    canonical = [canonical_signal_input(doc, yapo_parser) for doc in docs]
    by_identity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for data in canonical:
        key = publisher_identity_key(data)
        if key:
            by_identity[key].append(data)
    calculated_at = _now()
    plans = []
    for doc, data in zip(docs, canonical):
        identity_history = by_identity.get(publisher_identity_key(data), [])
        data["publisher_activity"] = compute_publisher_activity(
            data, identity_history, window_days=90, now=calculated_at
        )
        result = calculate_owner_score(data)
        plans.append(build_plan(
            doc, data, result, propose_classification_state(result), calculated_at
        ))

    summary = summarize(plans)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "summary": summary,
        "rows": [{key: value for key, value in plan.items() if key != "set"} for plan in plans],
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if args.apply:
        if not args.approved_backup or not args.approved_backup.is_file():
            raise RuntimeError("--apply requires --approved-backup")
        approved = json_util.loads(args.approved_backup.read_text(encoding="utf-8"))
        if approved.get("count") != len(docs):
            raise RuntimeError("Approved backup count does not match cohort")
        operations = [UpdateOne({"_id": plan["_id"]}, {"$set": plan["set"]}) for plan in plans]
        result = collection.bulk_write(operations, ordered=False)
        summary["apply_matched"] = result.matched_count
        summary["apply_modified"] = result.modified_count

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
