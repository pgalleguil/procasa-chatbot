"""Safely repair missing CRM owner mirrors on manually entered leads.

DRY-RUN by default. Only active, unique manual leads are eligible.
Requires --execute AND --expected-ready N for any writes.

Examples:
  python -m scripts.repair_manual_cycle_mirrors --owner-id <MongoObjectId> --since 2026-11-19
  python -m scripts.repair_manual_cycle_mirrors --owner-id <MongoObjectId> --since 2026-11-19 --expected-ready 5 --execute

Never reads WhatsApp content or modifies messages, assignment cycles, SLA,
campaigns or existing owner conflicts.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timezone
import json
import os

from bson import ObjectId

from chatbot.crm_manual_cycle_mirror import persist_manual_cycle_mirror
from chatbot.constants import CHILE_TZ
from chatbot.crm_assignment_integrity import assignment_context
from chatbot.executive_whatsapp_audit import _authorized_lead
from chatbot.phone_utils import has_real_phone, normalize_phone_strict, phone_identity_variants


def _since_utc(value: str) -> datetime:
    day = date.fromisoformat(value)
    return CHILE_TZ.localize(datetime.combine(day, time.min)).astimezone(timezone.utc)


def audit_and_repair(
    db, *, owner_id: str, since: datetime, execute: bool = False,
    expected_ready: int | None = None, max_cycles: int = 50,
) -> dict:
    if not ObjectId.is_valid(owner_id):
        return {"status": "BLOCKED", "reason": "invalid_owner_id"}
    owner = db["usuarios"].find_one({
        "_id": ObjectId(owner_id), "rol": "agente", "is_active": True,
    }, {"_id": 1, "nombre": 1})
    if not owner:
        return {"status": "BLOCKED", "reason": "owner_inactive_or_missing"}
    name = str(owner.get("nombre") or "").strip()
    if not name:
        return {"status": "BLOCKED", "reason": "owner_name_missing"}
    if not isinstance(since, datetime) or since.tzinfo is None:
        return {"status": "BLOCKED", "reason": "invalid_since"}
    if not 1 <= int(max_cycles) <= 100:
        return {"status": "BLOCKED", "reason": "invalid_limit"}

    query = {
        "assigned_to_user_id": owner_id, "reason": "manual_lead_created",
        "cycle_status": "active", "unassigned_at": None,
        "assigned_at": {"$gte": since},
    }
    rows = list(db["crm_assignment_cycles"].find(query, {
        "lead_id": 1, "assignment_cycle_id": 1, "assigned_to_user_id": 1,
        "assigned_to_display_name": 1, "cycle_status": 1,
        "unassigned_at": 1, "assigned_at": 1, "sla_started_at": 1,
    }).sort("assigned_at", -1).limit(int(max_cycles) + 1))
    if len(rows) > int(max_cycles):
        return {"status": "BLOCKED", "reason": "cycle_limit_exceeded"}

    checked = []
    ready_rows = []
    for cycle in rows:
        lid = cycle.get("lead_id")
        row = {
            "lead_last6": str(lid or "")[-6:],
            "status": "review_required",
        }
        if not isinstance(lid, ObjectId):
            row["reason"] = "unrecognized_id_type"
            checked.append(row)
            continue
        lead = db["leads"].find_one({"_id": lid}, {
            "_id": 1, "phone": 1, "source_type": 1,
            "lifecycle": 1, "ejecutivo_asignado": 1,
            "prospecto": 1, "assignment_mirror_owner_user_id": 1,
            "owner_user_id": 1, "assigned_to_user_id": 1,
        })
        if not lead or lead.get("source_type") != "manual":
            row["reason"] = "not_manual_canonical_lead"
            checked.append(row)
            continue
        phone = normalize_phone_strict(lead.get("phone"))
        variants = phone_identity_variants(phone) if phone else set()
        if not phone or not has_real_phone(phone) or not variants:
            row["reason"] = "incomplete_phone_identity"
            checked.append(row)
            continue
        if db["leads"].count_documents(
            {"phone": {"$in": list(variants)}}, limit=2,
        ) != 1:
            row["reason"] = "ambiguous_phone_identity"
            checked.append(row)
            continue

        lifecycle = lead.get("lifecycle") or {}
        cid = str(cycle.get("assignment_cycle_id") or "")
        already = (
            str(lifecycle.get("assigned_to_user_id") or "") == owner_id
            and str(lifecycle.get("current_assignment_cycle_id") or "") == cid
            and str(lifecycle.get("assignment_cycle_id") or "") == cid
        )
        if already:
            row["status"] = "already_correct"
        elif persist_manual_cycle_mirror(
            db, lead_id=lid, cycle=cycle, owner_name=name, dry_run=True,
        ):
            row["status"] = "ready"
            ready_rows.append((lid, cycle, phone))
        else:
            row["reason"] = "owner_or_cycle_conflict"
        checked.append(row)

    ready_count = len(ready_rows)
    review_count = sum(1 for x in checked if x["status"] == "review_required")
    report = {
        "status": "DRY_RUN" if not execute else "BLOCKED",
        "owner_user_id": owner_id, "since": since.isoformat(),
        "inspected": len(checked), "ready": ready_count,
        "already_correct": sum(1 for x in checked if x["status"] == "already_correct"),
        "review_required": review_count, "modified": 0,
        "leads": checked,
    }
    if not execute:
        return report
    if (
        expected_ready is None or int(expected_ready) < 1
        or ready_count != int(expected_ready) or review_count > 0
    ):
        report["reason"] = "explicit_expected_ready_mismatch_or_review"
        return report

    report["status"] = "COMPLETED"
    for lid, cycle, phone in ready_rows:
        if db["leads"].count_documents(
            {"phone": {"$in": list(phone_identity_variants(phone))}}, limit=2,
        ) != 1:
            report["status"] = "PARTIAL_FAILURE"
            report["reason"] = "phone_changed_before_write"
            break
        if not persist_manual_cycle_mirror(
            db, lead_id=lid, cycle=cycle, owner_name=name,
        ):
            report["status"] = "PARTIAL_FAILURE"
            report["reason"] = "mirror_compare_and_set_failed"
            break
        fresh = db["leads"].find_one({"_id": lid})
        if (
            not fresh
            or assignment_context(db, fresh).get("status") != "consistent"
            or _authorized_lead(db, phone=phone, user_id=owner_id) is None
        ):
            report["status"] = "PARTIAL_FAILURE"
            report["reason"] = "post_write_invariant_failed"
            break
        report["modified"] += 1
        for item in report["leads"]:
            if item["lead_last6"] == str(lid)[-6:]:
                item["status"] = "repaired_and_verified"
                break
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--since", required=True, help="YYYY-MM-DD in Chile")
    parser.add_argument("--max-cycles", type=int, default=50)
    parser.add_argument("--expected-ready", type=int)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        since = _since_utc(args.since)
    except ValueError:
        print(json.dumps({"status": "BLOCKED", "reason": "invalid_since"}))
        return 1
    env = os.getenv("PRICE_DROP_ENV_FILE") or ""
    from dotenv import load_dotenv
    if env:
        load_dotenv(env)
    else:
        load_dotenv()
    from chatbot.storage import get_db
    try:
        result = audit_and_repair(
            get_db(), owner_id=args.owner_id, since=since,
            execute=args.execute, expected_ready=args.expected_ready,
            max_cycles=args.max_cycles,
        )
    except Exception as exc:
        # Never log the connection string, customer data or WhatsApp payloads.
        result = {"status": "ERROR", "reason": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result["status"] in {"DRY_RUN", "COMPLETED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
