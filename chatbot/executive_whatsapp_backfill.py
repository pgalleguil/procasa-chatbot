"""Controlled migration of reviewed WhatsApp TXT exports into leads.

No new leads or collections are created. Import requires two independent
authorized reviewers and a checksum of the exact reviewed export. There is
NO endpoint and NO production caller; default mode is dry-run.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from bson import BSON, ObjectId

from .executive_whatsapp_audit import (
    BSON_ARRAY_ELEMENT_MARGIN,
    MAX_LEAD_BSON_BYTES,
    _find_cycle_for_message,
)

MAX_IMPORT_BATCH = 100


def _id(value: str) -> Any:
    try:
        return ObjectId(value)
    except (TypeError, ValueError):
        return value


def verify_import_manifest(parsed: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    if not isinstance(parsed, Mapping) or not isinstance(manifest, Mapping):
        raise ValueError("verified_export_manifest_required")
    source = parsed.get("source_sha256")
    if not isinstance(source, str) or len(source) != 64 or manifest.get("source_sha256") != source:
        raise ValueError("export_checksum_does_not_match")
    reviewers = manifest.get("reviewer_user_ids")
    if not isinstance(reviewers, list) or len(reviewers) != 2 or any(
        not isinstance(v, str) or not v.strip() for v in reviewers
    ) or reviewers[0] == reviewers[1]:
        raise ValueError("two_independent_reviewers_required")
    if manifest.get("business_content_only_verified") is not True:
        raise ValueError("business_only_conversation_review_required")
    if not isinstance(manifest.get("lead_id"), str) or not manifest["lead_id"].strip():
        raise ValueError("verified_lead_required")
    if not isinstance(manifest.get("executive_user_id"), str) or not manifest["executive_user_id"].strip():
        raise ValueError("verified_executive_required")


def import_reviewed_export(
    db: Any, *,
    parsed: Mapping[str, Any],
    manifest: Mapping[str, Any],
    commit: bool = False,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    """No lead updates before every row is checked against the verified cycle.

    A failed row is skipped, never reassigned heuristically. If a batch partially
    persists, rerun it: the per-message key is idempotent.
    """
    verify_import_manifest(parsed, manifest)
    rows = parsed.get("records")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_IMPORT_BATCH:
        raise ValueError("import_batch_must_contain_1_to_100_messages")
    now = captured_at or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("capture_time_requires_timezone")
    lead_id = _id(manifest["lead_id"])
    valid: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}

    for row in rows:
        if not isinstance(row, Mapping) or row.get("source_verified") is not False:
            raise ValueError("unexpected_export_record_provenance")
        if row.get("lead_id") != manifest["lead_id"] or row.get("executive_user_id") != manifest["executive_user_id"]:
            raise ValueError("export_record_binding_mismatch")
        ts = row.get("timestamp")
        if not isinstance(ts, datetime) or ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError("invalid_export_message_time")
        if row.get("actor_type") not in ("executive", "client"):
            raise ValueError("invalid_export_message_actor")
        body = row.get("content")
        if not isinstance(body, str) or not body.strip() or len(body) > 20_000:
            raise ValueError("invalid_export_message_body")
        evidence_key = row.get("evidence_key")
        if not isinstance(evidence_key, str) or not evidence_key.startswith("whatsapp-export:") or len(evidence_key) != 80:
            raise ValueError("invalid_export_evidence_key")
        lead, cycle, reason = _find_cycle_for_message(
            db, row.get("client_phone"), manifest["executive_user_id"], ts
        )
        if reason != "matched" or str(lead["_id"]) != str(lead_id):
            key = reason if reason != "matched" else "verified_lead_mismatch"
            rejected[key] = rejected.get(key, 0) + 1
            continue
        message = {
            "provider_key": evidence_key,
            "actor_type": row["actor_type"],
            "executive_user_id": manifest["executive_user_id"],
            "assignment_cycle_id": str(cycle["assignment_cycle_id"]),
            "session_id": row.get("session_id"),
            "channel": "executive_whatsapp",
            "message_type": "exported_text",
            "content": body,
            "timestamp": ts.astimezone(timezone.utc),
            "timestamp_origin": "verified_whatsapp_export_local_time",
            "timestamp_verified": False,
            "time_usable_for_sla": False,
            "cycle_attribution": "export_human_reviewed_cycle",
            "captured_at": now.astimezone(timezone.utc),
            "ingestion_source": "whatsapp_export_txt",
            "source_export_sha256": manifest["source_sha256"],
            "reviewer_user_ids": list(manifest["reviewer_user_ids"]),
        }
        valid.append(message)
    statuses: dict[str, int] = dict(rejected)
    if not commit:
        statuses["planned"] = len(valid)
        return {"mode": "dry_run", "results": statuses, "written": 0}

    writes = 0
    for message in valid:
        limit = MAX_LEAD_BSON_BYTES - len(BSON.encode(message)) - BSON_ARRAY_ELEMENT_MARGIN
        if limit <= 0:
            statuses["capacity_rejected"] = statuses.get("capacity_rejected", 0) + 1
            continue
        result = db["leads"].update_one(
            {
                "_id": lead_id,
                "executive_whatsapp_messages.provider_key": {"$ne": message["provider_key"]},
                "$expr": {"$lt": [{"$bsonSize": "$$ROOT"}, limit]},
            },
            {"$push": {"executive_whatsapp_messages": message}},
            upsert=False,
        )
        if result.modified_count == 1:
            writes += 1
            statuses["saved"] = statuses.get("saved", 0) + 1
            continue
        exists = db["leads"].find_one(
            {"_id": lead_id, "executive_whatsapp_messages.provider_key": message["provider_key"]},
            {"_id": 1}
        )
        name = "duplicate_confirmed" if exists else "write_unconfirmed"
        statuses[name] = statuses.get(name, 0) + 1
    return {"mode": "persist", "results": statuses, "written": writes}
