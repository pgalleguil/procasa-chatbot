"""Guarded legacy CRM mirror repair for active leads of an explicitly named executive.

READ-ONLY by default. Unlike the manual-only repair, this handles prior portal,
chatbot and manual leads. It never changes the authoritative assignment cycle,
commercial status, SLA, WhatsApp content, or other unrelated fields.

Mixed BSON dates and ISO-string assigned_at values are compared using their
actual instants, not MongoDB's type order. Only cycles BEFORE --before-utc are
eligible. All counts and the full audit fingerprint must match before writes.

Examples:
  python -m scripts.repair_legacy_executive_mirrors --owner-id OBJECT_ID --before-utc 2026-10-20T00:00:00Z
  python -m scripts.repair_legacy_executive_mirrors --owner-id OBJECT_ID --before-utc 2026-10-20T00:00:00Z --expected-ready 3 --expected-review 2 --expected-digest SHA256 --skip-reviewed --execute

No secrets, phone numbers or message content are printed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os

from bson import ObjectId
from dotenv import load_dotenv

load_dotenv(os.getenv("PRICE_DROP_ENV_FILE") or None)

from chatbot.crm_manual_cycle_mirror import persist_manual_cycle_mirror
from chatbot.crm_assignment_integrity import assignment_context
from chatbot.crm_metrics import coerce_utc_datetime
from chatbot.executive_whatsapp_audit import _authorized_lead
from chatbot.phone_utils import has_real_phone, normalize_phone_strict, phone_identity_variants


def _utc_cutoff(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("cutoff_needs_timezone")
    return parsed.astimezone(timezone.utc)


def audit_and_repair(
    db, *, owner_id: str, before: datetime,
    execute: bool = False, expected_ready: int | None = None,
    expected_review: int | None = None, expected_digest: str | None = None,
    skip_reviewed: bool = False, max_cycles: int = 500,
) -> dict:
    """Inspect a bounded, deterministic owner cohort and, optionally, fix safe leads."""
    if not ObjectId.is_valid(owner_id):
        return {"status": "BLOCKED", "reason": "invalid_owner_id"}
    if not isinstance(before, datetime) or before.tzinfo is None:
        return {"status": "BLOCKED", "reason": "invalid_cutoff"}
    before = before.astimezone(timezone.utc)
    if not 1 <= int(max_cycles) <= 1000:
        return {"status": "BLOCKED", "reason": "invalid_cycle_limit"}
    owner = db["usuarios"].find_one({
        "_id": ObjectId(owner_id), "is_active": True, "rol": "agente",
    }, {"_id": 1, "nombre": 1})
    if not owner or not str(owner.get("nombre") or "").strip():
        return {"status": "BLOCKED", "reason": "owner_inactive_or_missing"}
    owner_name = str(owner["nombre"]).strip()
    # Read all active cycles belonging to this executive, including string-date
    # legacy cycles. Never filter assigned_at by BSON type ordering.
    rows = list(db["crm_assignment_cycles"].find({
        "assigned_to_user_id": owner_id,
        "cycle_status": "active",
        "unassigned_at": None,
    }, {
        "_id": 1, "lead_id": 1, "assignment_cycle_id": 1,
        "assigned_to_user_id": 1, "assigned_to_display_name": 1,
        "cycle_status": 1, "unassigned_at": 1, "assigned_at": 1,
        "cycle_started_at": 1, "sla_started_at": 1, "property_code": 1,
        "reassignment_state": 1,
    }).sort("_id", 1).limit(int(max_cycles) + 1))
    if len(rows) > max_cycles:
        return {"status": "BLOCKED", "reason": "cycle_limit_exceeded"}

    checked = []
    ready_rows = []
    digest_items = []

    for cycle in rows:
        instant = coerce_utc_datetime(cycle.get("assigned_at"))
        if instant is not None and instant >= before:
            continue
        lid = cycle.get("lead_id")
        cid = str(cycle.get("assignment_cycle_id") or "")
        item = {"lead_last6": str(lid or "")[-6:], "status": "review_required"}
        reason = None
        phone = None
        if instant is None:
            reason = "unparseable_assignment_date"
        elif not isinstance(lid, ObjectId):
            reason = "unsupported_lead_identity"
        else:
            lead = db["leads"].find_one({"_id": lid}, {
                "_id": 1, "phone": 1, "source_type": 1,
                "ejecutivo_asignado": 1, "prospecto": 1, "lifecycle": 1,
                "owner_user_id": 1, "assigned_to_user_id": 1,
                "assignment_mirror_owner_user_id": 1,
            })
            if not lead:
                reason = "canonical_lead_missing"
            else:
                lifecycle = lead.get("lifecycle") or {}
                already = (
                    isinstance(lifecycle, dict)
                    and str(lifecycle.get("assigned_to_user_id") or "") == owner_id
                    and str(lifecycle.get("current_assignment_cycle_id") or "") == cid
                    and str(lifecycle.get("assignment_cycle_id") or "") == cid
                )
                # Even already-correct fields require a unique canonical chat identity
                # before they can be counted as auditable.
                phone = normalize_phone_strict(lead.get("phone"))
                variants = phone_identity_variants(phone) if phone else []
                if not phone or not has_real_phone(lead.get("phone")) or not variants:
                    reason = "invalid_or_synthetic_phone"
                elif db["leads"].count_documents(
                    {"phone": {"$in": list(variants)}}, limit=2
                ) != 1:
                    reason = "ambiguous_phone_identity"
                elif already and assignment_context(db, lead).get("status") == "consistent":
                    item["status"] = "already_correct"
                elif persist_manual_cycle_mirror(
                    db, lead_id=lid, cycle=cycle,
                    owner_name=owner_name, dry_run=True,
                ):
                    # Simulate only the fields the shared CAS helper would set.
                    prospective = dict(lead)
                    prospective_lc = dict(lifecycle)
                    prospective_lc.update({
                        "assigned_to_user_id": owner_id,
                        "current_assignment_cycle_id": cid,
                        "assignment_cycle_id": cid,
                    })
                    prospective["lifecycle"] = prospective_lc
                    if assignment_context(
                        db, prospective, active_cycles_override=[cycle],
                    ).get("status") == "consistent":
                        item["status"] = "ready"
                        ready_rows.append((len(checked), lid, cycle, phone))
                    else:
                        reason = "assignment_integrity_conflict"
                else:
                    reason = "owner_or_cycle_conflict"
        if reason:
            item["reason"] = reason
        checked.append(item)
        digest_items.append(
            "|".join([str(lid), cid, item["status"], str(item.get("reason") or "")])
        )

    ready_count = len(ready_rows)
    review_count = sum(x["status"] == "review_required" for x in checked)
    fingerprint = sha256(("\n".join(digest_items)).encode("utf-8")).hexdigest()
    report = {
        "status": "DRY_RUN" if not execute else "BLOCKED",
        "owner_user_id": owner_id,
        "before_utc": before.isoformat(),
        "inspected": len(checked),
        "ready": ready_count,
        "already_correct": sum(x["status"] == "already_correct" for x in checked),
        "review_required": review_count,
        "modified": 0,
        "review_skipped": 0,
        "digest": fingerprint,
        "leads": checked,
    }
    if not execute:
        return report
    # Both the totals AND the complete identity/status fingerprint must match.
    # A second execution will normally have a different fingerprint/ready count.
    if (
        expected_ready is None or int(expected_ready) < 1
        or int(expected_ready) != ready_count
        or expected_review is None or int(expected_review) != review_count
        or not isinstance(expected_digest, str)
        or len(expected_digest) != 64 or expected_digest != fingerprint
        or (review_count > 0 and not skip_reviewed)
    ):
        report["reason"] = "expected_counts_digest_or_review_mismatch"
        return report
    report["status"] = "COMPLETED"
    report["review_skipped"] = review_count
    for idx, lid, cycle, phone in ready_rows:
        # Re-check identity immediately before any change. CAS also verifies
        # the owner/cycle mirrors have not changed concurrently.
        if db["leads"].count_documents(
            {"phone": {"$in": list(phone_identity_variants(phone))}}, limit=2
        ) != 1:
            report["status"] = "PARTIAL_FAILURE"
            report["reason"] = "phone_identity_changed"
            break
        if not persist_manual_cycle_mirror(
            db, lead_id=lid, cycle=cycle, owner_name=owner_name,
        ):
            report["status"] = "PARTIAL_FAILURE"
            report["reason"] = "mirror_cas_failed"
            break
        fresh = db["leads"].find_one({"_id": lid})
        if (
            not fresh
            or assignment_context(db, fresh).get("status") != "consistent"
            or _authorized_lead(db, phone=phone, user_id=owner_id) is None
        ):
            report["status"] = "PARTIAL_FAILURE"
            report["reason"] = "post_write_verification_failed"
            break
        checked[idx]["status"] = "repaired_and_verified"
        report["modified"] += 1
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--before-utc", required=True)
    parser.add_argument("--max-cycles", type=int, default=500)
    parser.add_argument("--expected-ready", type=int)
    parser.add_argument("--expected-review", type=int)
    parser.add_argument("--expected-digest")
    parser.add_argument("--skip-reviewed", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        before = _utc_cutoff(args.before_utc)
    except (ValueError, TypeError):
        print(json.dumps({"status": "BLOCKED", "reason": "invalid_cutoff"}))
        return 1
    try:
        from chatbot.storage import get_db
        result = audit_and_repair(
            get_db(), owner_id=args.owner_id, before=before,
            max_cycles=args.max_cycles, expected_ready=args.expected_ready,
            expected_review=args.expected_review, expected_digest=args.expected_digest,
            skip_reviewed=args.skip_reviewed, execute=args.execute,
        )
    except Exception as exc:
        result = {"status": "ERROR", "reason": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result["status"] in {"DRY_RUN", "COMPLETED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
