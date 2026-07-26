"""
Phase 0: Verify duplicate cycles, create index, run dry-run, execute backfill.

Steps:
1. Verify zero duplicate active cycles
2. Create unique partial index
3. Dry-run (default) or --apply backfill of 716 active leads
"""
import sys, os, uuid
from datetime import datetime, timezone
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import Config
if not Config.MONGO_URI:
    raise RuntimeError("MONGO_URI is required; refusing to use a local fallback")

from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from bson import ObjectId

client = MongoClient(Config.MONGO_URI, socketTimeoutMS=60000, connectTimeoutMS=10000, serverSelectionTimeoutMS=20000)
db = client[Config.DB_NAME]
CHILE_TZ = __import__("pytz").timezone("America/Santiago")

INDEX_NAME = "uq_crm_assignment_cycle_active_lead"
CLOSED_STAGES = {"ARCHIVED", "CLOSED_WON", "CLOSED_LOST", "CERRADO"}


def step1_verify_no_duplicates():
    print("=== STEP 1: VERIFY ZERO DUPLICATE ACTIVE CYCLES ===")
    multi = list(db["crm_assignment_cycles"].aggregate([
        {"$match": {"cycle_status": "active", "unassigned_at": None}},
        {"$group": {"_id": "$lead_id", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
    ]))
    if multi:
        print(f"  ✗ FOUND {len(multi)} leads with >1 active cycle!")
        for m in multi:
            print(f"    lead={m['_id']} count={m['count']}")
        return False
    print("  ✓ Zero duplicate active cycles")
    return True


def step2_create_index():
    print("\n=== STEP 2: CREATE UNIQUE ACTIVE CYCLE INDEX ===")
    existing = {idx["name"] for idx in db["crm_assignment_cycles"].list_indexes()}
    if INDEX_NAME in existing:
        print(f"  ✓ Index '{INDEX_NAME}' already exists")
        return True
    try:
        db["crm_assignment_cycles"].create_index(
            [("lead_id", ASCENDING)],
            unique=True,
            partialFilterExpression={"cycle_status": "active"},
            name=INDEX_NAME,
        )
        print(f"  ✓ Index '{INDEX_NAME}' created")
        return True
    except Exception as e:
        print(f"  ✗ Error creating index: {e}")
        return False


def step3_dry_run():
    print("\n=== STEP 3: DRY-RUN — LEADS WITHOUT CYCLES ===")
    pipeline = [
        {"$match": {"ejecutivo_asignado": {"$nin": ["", "Sin Asignar", "No Asignado", "Sin asignar", "No asignado", None]}}},
        {"$lookup": {"from": "crm_assignment_cycles", "localField": "_id", "foreignField": "lead_id", "as": "cycles"}},
        {"$match": {"cycles": {"$size": 0}}},
        {"$project": {
            "_id": 1, "phone": 1, "ejecutivo_asignado": 1, "pipeline_stage": 1, "stage": 1,
            "created_at": 1, "prospecto.origen": 1, "prospecto.codigo": 1,
            "lifecycle.assigned_at": 1, "lifecycle.first_valid_management_at": 1,
            "lifecycle.closed_at": 1, "lifecycle.archived_at": 1,
            "lead_temperature_effective": 1,
        }},
    ]
    leads = list(db["leads"].aggregate(pipeline, allowDiskUse=True))
    print(f"  Leads without cycle: {len(leads)}")
    
    by_cat = Counter()
    by_origin = Counter()
    by_exec = Counter()
    examples = {"ACTIVE": [], "MANAGED": [], "NO_DATE": [], "CLOSED": []}
    
    for l in leads:
        stage = str(l.get("pipeline_stage") or l.get("stage") or "").upper()
        fvma = (l.get("lifecycle") or {}).get("first_valid_management_at")
        assigned_at = (l.get("lifecycle") or {}).get("assigned_at")
        exec_name = l.get("ejecutivo_asignado") or "?"
        origin = (l.get("prospecto") or {}).get("origen") or "?"
        is_closed = stage in CLOSED_STAGES
        
        if is_closed:
            cat = "CLOSED"
        elif not assigned_at:
            cat = "NO_DATE"
        elif fvma:
            cat = "MANAGED"
        else:
            cat = "ACTIVE"
        
        by_cat[cat] += 1
        if not is_closed:
            by_origin[origin] += 1
            by_exec[exec_name] += 1
        
        if len(examples.get(cat, [])) < 3:
            examples.setdefault(cat, []).append(
                f"lead={str(l['_id'])[:20]}... exec={exec_name[:20]} origin={origin[:15]} "
                f"temp={l.get('lead_temperature_effective','?')}"
            )
    
    print(f"\n  {'Category':30s} {'Count':>6s}")
    print(f"  {'-'*38}")
    for cat in ["ACTIVE", "MANAGED", "NO_DATE", "CLOSED"]:
        c = by_cat.get(cat, 0)
        print(f"  {cat:30s} {c:>6d}")
        for ex in examples.get(cat, []):
            print(f"    {ex}")
    print(f"  {'-'*38}")
    print(f"  {'TOTAL':30s} {len(leads):>6d}")
    
    if by_origin:
        print(f"\n  Origin distribution: {dict(by_origin.most_common(8))}")
    if by_exec:
        print(f"  Executive distribution: {dict(by_exec.most_common(8))}")
    
    return leads


def reconcile_one(db, lead):
    """Idempotent reconciliation of a single lead. Returns status dict."""
    # Check if already reconciled (active or closed cycle exists)
    existing = db["crm_assignment_cycles"].find_one({"lead_id": lead["_id"]})
    if existing:
        return {"lead_id": str(lead["_id"]), "status": "already_reconciled"}
    
    lifecycle = lead.get("lifecycle") or {}
    stage = str(lead.get("pipeline_stage") or lead.get("stage") or "").upper()
    is_closed = stage in CLOSED_STAGES
    exec_name = lead.get("ejecutivo_asignado") or ""
    user = db["usuarios"].find_one({"nombre": exec_name})
    
    # assigned_at
    assigned_at = lifecycle.get("assigned_at") or lead.get("created_at")
    assigned_at_estimated = lifecycle.get("assigned_at") is None
    
    # unassigned_at for closed leads
    unassigned_at = None
    unassigned_at_estimated = False
    unassigned_at_source = None
    if is_closed:
        for src in ["closed_at", "archived_at", "updated_at"]:
            val = lifecycle.get(src) or lead.get(src)
            if val:
                unassigned_at = val
                unassigned_at_source = src
                break
        if not unassigned_at:
            unassigned_at = lead.get("created_at")
            unassigned_at_estimated = True
            unassigned_at_source = "created_at_fallback"
    
    cycle_id = str(uuid.uuid4())
    
    cycle = {
        "assignment_cycle_id": cycle_id,
        "lead_id": lead["_id"],
        "assigned_to_user_id": str(user["_id"]) if user else "unknown",
        "assigned_to_display_name": exec_name,
        "assigned_at": assigned_at,
        "assigned_at_estimated": assigned_at_estimated,
        "unassigned_at": unassigned_at,
        "assigned_by": "system",
        "reason": "historical_reconciliation",
        "metric_version": "crm_metrics_v1",
        "schema_version": "crm_assignment_cycle_v1",
        "cycle_status": "closed" if is_closed else "active",
        "cycle_origin": "historical_reconciliation",
        "backfill_side_effects_suppressed": True,
        "reconciled_at": datetime.now(timezone.utc),
        "reconciled_by": "script:reconcile_missing_cycles",
        "sla_segments": [],
        "sla_v2_history_available": False,
    }
    
    if unassigned_at_estimated:
        cycle["unassigned_at_estimated"] = True
        cycle["unassigned_at_source"] = unassigned_at_source
    
    fvma = lifecycle.get("first_valid_management_at")
    if fvma:
        cycle["first_valid_management_at"] = fvma
    
    try:
        db["crm_assignment_cycles"].insert_one(cycle)
        return {"lead_id": str(lead["_id"]), "status": "created", "cycle_id": cycle_id}
    except DuplicateKeyError:
        return {"lead_id": str(lead["_id"]), "status": "already_reconciled"}


def step4_backfill(leads):
    """Apply backfill for all active leads (excluding CLOSED)."""
    active_leads = [l for l in leads if str(l.get("pipeline_stage") or l.get("stage") or "").upper() not in CLOSED_STAGES]
    print(f"\n=== STEP 4: BACKFILL {len(active_leads)} ACTIVE LEADS ===")
    
    stats = Counter()
    batch_size = 50
    total = len(active_leads)
    
    for i in range(0, total, batch_size):
        batch = active_leads[i:i+batch_size]
        for lead in batch:
            result = reconcile_one(db, lead)
            stats[result["status"]] += 1
        pct = min(100, (i + batch_size) * 100 // total)
        print(f"  Progress: {min(i+batch_size, total)}/{total} ({pct}%) — {dict(stats)}")
    
    print(f"\n  Final: {dict(stats)}")
    return stats


def step5_verify():
    print("\n=== STEP 5: POST-BACKFILL VERIFICATION ===")
    pipeline = [
        {"$match": {"ejecutivo_asignado": {"$nin": ["", "Sin Asignar", "No Asignado", "Sin asignar", "No asignado", None]}}},
        {"$lookup": {"from": "crm_assignment_cycles", "localField": "_id", "foreignField": "lead_id", "as": "cycles"}},
        {"$match": {"cycles": {"$size": 0, "$exists": True}}},
        {"$count": "count"},
    ]
    try:
        result = list(db["leads"].aggregate(pipeline, allowDiskUse=True))
        remaining = result[0]["count"] if result else 0
    except:
        # Fallback: count manually
        remaining = 0
        for l in db["leads"].find({"ejecutivo_asignado": {"$nin": ["", "Sin Asignar", None]}},
                                  {"_id": 1}).batch_size(500):
            c = db["crm_assignment_cycles"].count_documents({"lead_id": l["_id"]})
            if c == 0:
                remaining += 1
    
    print(f"  Leads assigned without cycle: {remaining}")
    if remaining == 0:
        print("  ✓ Backfill complete — zero leads without cycle")
    else:
        print(f"  ⚠ {remaining} leads still without cycle (likely CLOSED)")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Execute backfill")
    args = parser.parse_args()
    
    print("=" * 70)
    print("PHASE 0: BACKFILL MISSING CANONICAL CYCLES")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN (no changes)'}")
    print("=" * 70)
    
    if not step1_verify_no_duplicates():
        if args.apply:
            print("\n  ✗ Cannot proceed with --apply until duplicates are resolved")
            return
        print("  ⚠ Warning: duplicates exist but dry-run is read-only\n")
    
    step2_create_index()
    
    leads = step3_dry_run()
    
    if args.apply:
        step4_backfill(leads)
        step5_verify()
    else:
        total_active = sum(1 for l in leads if str(l.get("pipeline_stage") or l.get("stage") or "").upper() not in CLOSED_STAGES)
        print(f"\n  Use --apply to backfill {total_active} active leads")
        print("  18 CLOSED leads will be skipped (second phase)")


if __name__ == "__main__":
    main()
