"""Dry-run: detect active leads with assigned executive but no canonical cycle.

Usage:
    python scripts/detect_leads_without_cycles.py           # dry-run (default)
    python scripts/detect_leads_without_cycles.py --verbose  # show lead details
"""
from __future__ import annotations

import argparse
import sys
import os
from datetime import datetime, timezone
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("MONGO_URI", "mongodb+srv://pgalleguil:vLr5MTTZ7kcNzjSZ@cluster0.mzve39k.mongodb.net/?retryWrites=true&w=majority")
os.environ.setdefault("DB_NAME", "URLS")

from pymongo import MongoClient
from bson import ObjectId

client = MongoClient(
    os.environ["MONGO_URI"],
    socketTimeoutMS=30000,
    connectTimeoutMS=10000,
    serverSelectionTimeoutMS=20000,
)
db = client[os.environ["DB_NAME"]]

UNASSIGNED_VALUES = {"", "Sin Asignar", "No Asignado", "Sin asignar", "No asignado", None}
ACTIVE_STAGES = {"ARCHIVED", "CLOSED_WON", "CLOSED_LOST"}


def find_leads_without_cycles(db, verbose=False) -> dict:
    """Find active leads that have an ejecutivo_asignado but no crm_assignment_cycles."""
    query = {
        "ejecutivo_asignado": {"$nin": list(UNASSIGNED_VALUES)},
        "pipeline_stage": {"$nin": list(ACTIVE_STAGES)},
    }
    total = db["leads"].count_documents(query)
    print(f"Total leads with assigned exec (not closed): {total}")

    missing_cycle = []
    multiple_cycles = []
    cycle_mismatch = []
    by_origin = Counter()

    batch_size = 200
    cursor = db["leads"].find(
        query,
        {"_id": 1, "phone": 1, "ejecutivo_asignado": 1, "prospecto.origen": 1,
         "prospecto.codigo": 1, "lifecycle": 1, "created_at": 1},
    ).batch_size(batch_size)

    for lead in cursor:
        lid = lead["_id"]
        origin = (lead.get("prospecto") or {}).get("origen") or "?"
        exec_name = lead.get("ejecutivo_asignado") or "?"

        cycles = list(db["crm_assignment_cycles"].find(
            {"lead_id": lid},
            {"assignment_cycle_id": 1, "assigned_to_user_id": 1,
             "assigned_to_display_name": 1, "cycle_status": 1, "unassigned_at": 1},
        ).sort("assigned_at", -1))

        if not cycles:
            missing_cycle.append({
                "lead_id": str(lid),
                "phone": lead.get("phone", "?")[-4:],
                "executive": exec_name,
                "origin": origin,
                "property": (lead.get("prospecto") or {}).get("codigo") or "?",
                "created_at": lead.get("created_at"),
            })
            by_origin[origin] += 1
            if verbose:
                print(f"  NO CYCLE: lead={lid} exec={exec_name} origin={origin} phone=****{lead.get('phone','?')[-4:]}")
        elif len(cycles) > 1:
            active = [c for c in cycles if c.get("cycle_status") == "active" and not c.get("unassigned_at")]
            if len(active) > 1:
                multiple_cycles.append({"lead_id": str(lid), "active_cycles": len(active)})
                if verbose:
                    print(f"  MULTI ACTIVE: lead={lid} exec={exec_name} active_cycles={len(active)}")
            if active:
                cycle_exec = active[0].get("assigned_to_display_name") or active[0].get("assigned_to_user_id") or "?"
                if str(cycle_exec) != str(exec_name):
                    cycle_mismatch.append({"lead_id": str(lid), "cycle_exec": str(cycle_exec), "lead_exec": exec_name})
                    if verbose:
                        print(f"  MISMATCH: lead={lid} cycle_exec={cycle_exec} lead_exec={exec_name}")

    return {
        "total_leads": total,
        "missing_cycle": missing_cycle,
        "missing_count": len(missing_cycle),
        "multiple_active": multiple_cycles,
        "multiple_count": len(multiple_cycles),
        "cycle_mismatch": cycle_mismatch,
        "mismatch_count": len(cycle_mismatch),
        "by_origin": dict(by_origin),
    }


def main():
    parser = argparse.ArgumentParser(description="Detect leads without canonical assignment cycles")
    parser.add_argument("--verbose", action="store_true", help="Show lead details")
    args = parser.parse_args()

    print("=" * 70)
    print("DRY-RUN: Leads sin ciclo canónico de asignación")
    print("=" * 70)

    result = find_leads_without_cycles(db, verbose=args.verbose)

    print(f"\n=== RESUMEN ===")
    print(f"Leads con ejecutivo asignado (activos): {result['total_leads']}")
    print(f"  Sin ciclo canónico: {result['missing_count']}")
    print(f"  Múltiples ciclos activos: {result['multiple_count']}")
    print(f"  Ciclo vs lead ejecutivo mismatch: {result['mismatch_count']}")

    if result["missing_cycle"]:
        print(f"\n=== DISTRIBUCIÓN POR ORIGEN (leads sin ciclo) ===")
        for origin, count in sorted(result["by_origin"].items(), key=lambda x: -x[1]):
            print(f"  {origin}: {count}")

        if not args.verbose:
            print(f"\nPrimeros 5 leads sin ciclo:")
            for lead in result["missing_cycle"][:5]:
                print(f"  lead={lead['lead_id'][:20]}... exec={lead['executive']} origin={lead['origin']} "
                      f"prop={lead['property']} created={lead['created_at']}")

        print(f"\n  Use --verbose para ver todos los detalles.")

    print(f"\n=== ACCIÓN RECOMENDADA ===")
    print(f"Revisar y ejecutar reconciliación que cree ciclos faltantes")
    print(f"manteniendo la fecha histórica de asignación original.")
    print(f"Usar lifecycle.assigned_at como assigned_at del ciclo si existe.")
    print(f"Sugerencia: ejecutar en horario de baja afluencia.")


if __name__ == "__main__":
    main()
