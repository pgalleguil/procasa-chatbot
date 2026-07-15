#!/usr/bin/env python
"""Backup, apply or rollback the approved targeted owner-probability run."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bson import json_util
from chatbot.storage import get_db
from config import Config

RUN_VERSION = "owner-probability-evidence-v1"
RULE_VERSION = "owner-probability-band-state-v1"


def _query(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"$or": [{"origen": row["origen"], "listing_id": row["listing_id"]} for row in rows]}


def _backup(collection, rows: list[dict[str, Any]], output_dir: Path, stamp: str) -> tuple[Path, Path]:
    docs = list(collection.find(_query(rows)))
    backup_path = output_dir / f"owner_probability_targeted_backup_{stamp}.json"
    payload = json_util.dumps(docs, ensure_ascii=False, indent=2)
    backup_path.write_text(payload, encoding="utf-8")
    digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    parsed = json_util.loads(backup_path.read_text(encoding="utf-8"))
    expected = {(row["origen"], row["listing_id"]) for row in rows}
    actual = {(str(doc.get("origen")), str(doc.get("listing_id"))) for doc in parsed}
    if expected != actual:
        raise RuntimeError(f"Backup round-trip mismatch: missing={len(expected-actual)} extra={len(actual-expected)}")
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "documents": len(docs), "sha256": digest,
        "source": str(backup_path), "round_trip_verified": True,
        "fields_protected": ["classification", "gestion", "assignment", "previous_state", "previous_scores"],
    }
    manifest_path = backup_path.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return backup_path, manifest_path


def _trace_map(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    result = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            item = json.loads(line)
            result[(str(item.get("origen")), str(item.get("listing_id")))] = item
    return result


def apply(report_path: Path, traces_path: Path, output_dir: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    rows = report["rows"]
    collection = Config.get_captacion_collection(get_db())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path, manifest_path = _backup(collection, rows, output_dir, stamp)
    traces = _trace_map(traces_path)
    run_id = f"owner_probability_targeted_{stamp}"
    stats = {"updated": 0, "pending": 0, "removed": 0, "desassigned_broker": 0, "skipped_idempotent": 0, "errors": []}

    for row in rows:
        key = (row["origen"], row["listing_id"])
        current = collection.find_one({"origen": key[0], "listing_id": key[1]})
        if not current:
            stats["errors"].append({"key": key, "error": "not_found"})
            continue
        cls = current.get("classification") or {}
        if cls.get("owner_probability_run_id") == run_id:
            stats["skipped_idempotent"] += 1
            continue
        previous_state = cls.get("state") or cls.get("final_state") or ""
        proposed = row["estado_propuesto"]
        percentage = row.get("porcentaje")
        probability = row.get("owner_probability")
        applied = row.get("senales") or []
        max_strength = max((abs(int(signal.get("weight") or 0)) for signal in applied), default=0)
        evidence_quality = (
            "INCOMPLETE" if probability is None
            else "COMPLETE_NEUTRAL" if not applied
            else "COMPLETE_STRONG_EVIDENCE" if max_strength >= 35
            else "COMPLETE_PARTIAL_EVIDENCE"
        )
        trace = traces.get(key) or {}
        now = datetime.now(timezone.utc)
        set_fields: dict[str, Any] = {
            "classification.previous_classification_state": previous_state,
            "classification.state": proposed,
            "classification.final_state": proposed,
            "classification.owner_probability": probability,
            "classification.owner_probability_signals": {
                "base": 50, "applied": applied,
                "raw_score": percentage, "neutral": not applied,
                "family_rule": "one strongest signal per family",
            },
            "classification.owner_probability_version": RUN_VERSION,
            "classification.owner_probability_calculated_at": now,
            "classification.owner_probability_evidence_quality": evidence_quality,
            "classification.owner_probability_source": "deterministic_evidence_engine",
            "classification.classification_rule_version": RULE_VERSION,
            "classification.owner_probability_run_id": run_id,
            "classification.transition_reason": row.get("transition_reason") or "owner_probability_band_v1",
            "classification.deepseek_structured_evidence_status": row.get("deepseek_status"),
            "classification.deepseek_structured_evidence": trace.get("evidence") or [],
            "classification.deepseek_raw": trace.get("raw") or cls.get("deepseek_raw") or {},
            "classification.deepseek_message_content": trace.get("message_content") or "",
            "classification.deepseek_reasoning_content": trace.get("reasoning_content") or "",
            "classification.deepseek_payload": trace.get("payload") or {},
            "classification.prompt_version": trace.get("prompt_version") or "owner-evidence-deepseek-v1",
            "classification.manual_review_required": proposed == "PENDIENTE",
            "classification.assignment_ready": probability is not None and probability >= 0.5 and proposed not in {"AD_REMOVED", "PENDIENTE"},
            "classification.exclude_from_assignment": probability is None or probability < 0.5 or proposed in {"AD_REMOVED", "PENDIENTE"},
            "gestion.semantic_review_hold": proposed == "PENDIENTE",
            "updated_at": now,
        }
        if proposed == "PENDIENTE":
            stats["pending"] += 1
            set_fields["classification.assignment_block_reasons"] = row.get("motivos_pendiente") or ["owner_probability_pending"]
        elif proposed == "AD_REMOVED":
            stats["removed"] += 1
            set_fields["scrape_stage"] = "ad_removed"
            set_fields["classification.assignment_block_reasons"] = ["removed_listing"]
        elif probability is not None and probability < 0.5:
            set_fields["classification.assignment_block_reasons"] = ["owner_probability_below_50"]
        else:
            set_fields["classification.assignment_block_reasons"] = []

        update: dict[str, Any] = {"$set": set_fields}
        if row.get("asignada_pasa_corredor"):
            gestion = current.get("gestion") or {}
            removal_event = {
                "removed_at": now,
                "reason": "owner_probability_below_50",
                "owner_probability": probability,
                "new_state": proposed,
                "previous_state": previous_state,
                "ejecutivo_id": gestion.get("ejecutivo_id"),
                "ejecutivo_nombre": gestion.get("ejecutivo_asignado") or gestion.get("ejecutivo_nombre"),
                "evidence": applied,
                "run_id": run_id,
            }
            set_fields.update({
                "gestion.previous_assignment_before_owner_probability": {
                    "ejecutivo_id": gestion.get("ejecutivo_id"),
                    "ejecutivo_asignado": gestion.get("ejecutivo_asignado"),
                    "ejecutivo_email": gestion.get("ejecutivo_email"),
                },
                "gestion.ejecutivo_id": None,
                "gestion.ejecutivo_asignado": None,
                "gestion.ejecutivo_email": None,
                "gestion.desasignada_por_owner_probability": True,
                "gestion.fecha_desasignacion": now,
            })
            update["$push"] = {"gestion.historial_desasignaciones": removal_event}
            stats["desassigned_broker"] += 1

        result = collection.update_one(
            {"_id": current["_id"], "classification.owner_probability_run_id": {"$ne": run_id}},
            update,
        )
        stats["updated"] += result.modified_count

    stats.update({
        "run_id": run_id, "backup": str(backup_path), "manifest": str(manifest_path),
        "report": str(report_path), "traces": str(traces_path),
    })
    application_path = output_dir / f"owner_probability_targeted_application_{stamp}.json"
    application_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    stats["application_report"] = str(application_path)
    return stats


def rollback(backup_path: Path) -> dict[str, Any]:
    docs = json_util.loads(backup_path.read_text(encoding="utf-8"))
    collection = Config.get_captacion_collection(get_db())
    restored = 0
    for doc in docs:
        doc_id = doc.pop("_id")
        result = collection.replace_one({"_id": doc_id}, doc, upsert=False)
        restored += result.modified_count
    return {"backup": str(backup_path), "restored": restored, "expected": len(docs)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", default="")
    parser.add_argument("--report", default=str(ROOT / "reports" / "owner_probability_targeted_dry_run_20260715_004216.json"))
    parser.add_argument("--traces", default=str(ROOT / "reports" / "owner_probability_targeted_deepseek_traces_20260715_004216.jsonl"))
    parser.add_argument("--output-dir", default=str(ROOT / "backups"))
    args = parser.parse_args()
    if args.rollback:
        result = rollback(Path(args.rollback))
    elif args.apply:
        result = apply(Path(args.report), Path(args.traces), Path(args.output_dir))
    else:
        raise RuntimeError("Use --apply o --rollback BACKUP")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
