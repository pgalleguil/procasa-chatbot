"""Reconcilia vigencias migradas con la primera asignaciÃ³n operativa verificable."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from captacion_workforce import MEMBERSHIP_COLLECTION, WORKFORCE_AUDIT_COLLECTION
from chatbot.storage import get_db
from config import Config


CHILE = ZoneInfo("America/Santiago")
MIGRATION_VERSION = "workforce_start_reconciliation_v1"


def local_date(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CHILE).date().isoformat()


def build_plan(db) -> list[dict]:
    collection = Config.get_captacion_collection(db)
    plan = []
    for membership in db[MEMBERSHIP_COLLECTION].find({"enabled": True}).sort("user_id", 1):
        user_id = str(membership.get("user_id") or "")
        user = db["usuarios"].find_one({"_id": membership.get("user_id")})
        if not user:
            try:
                from bson import ObjectId
                user = db["usuarios"].find_one({"_id": ObjectId(user_id)})
            except Exception:
                user = None
        name = (user or {}).get("nombre") or membership.get("name_snapshot")
        clauses = [{"gestion.ejecutivo_id": user_id}]
        if name:
            clauses.append({"gestion.ejecutivo_asignado": name})
        first = collection.find_one(
            {"$or": clauses, "gestion.fecha_asignacion": {"$type": "date"}},
            {"gestion.fecha_asignacion": 1},
            sort=[("gestion.fecha_asignacion", 1)],
        )
        assigned_at = ((first or {}).get("gestion") or {}).get("fecha_asignacion")
        if not assigned_at:
            continue
        inferred = local_date(assigned_at)
        current = str(membership.get("start_date") or "")
        if current and inferred >= current:
            continue
        plan.append({
            "user_id": user_id,
            "name": name,
            "previous_start_date": current,
            "inferred_start_date": inferred,
            "evidence": "gestion.fecha_asignacion",
        })
    return plan


def backup(db) -> Path:
    path = Path("backups") / f"captacion_team_memberships_pre_date_reconciliation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(db[MEMBERSHIP_COLLECTION].find({}))
    path.write_text(json.dumps(rows, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor-user-id", default=f"migration:{MIGRATION_VERSION}")
    args = parser.parse_args()
    db = get_db()
    plan = build_plan(db)
    result = {"mode": "apply" if args.apply else "preview", "migration_version": MIGRATION_VERSION, "changes": plan}
    if args.apply and plan:
        result["backup"] = str(backup(db))
        now = datetime.now(timezone.utc)
        for item in plan:
            db[MEMBERSHIP_COLLECTION].update_one(
                {"user_id": item["user_id"], "start_date": item["previous_start_date"]},
                {"$set": {
                    "start_date": item["inferred_start_date"],
                    "start_date_reconciled_at": now,
                    "start_date_reconciliation_version": MIGRATION_VERSION,
                    "updated_at": now,
                    "updated_by": args.actor_user_id,
                }},
            )
            db[WORKFORCE_AUDIT_COLLECTION].insert_one({
                "event_type": "membership_start_date_reconciled",
                "user_id": item["user_id"],
                "actor_user_id": args.actor_user_id,
                "previous_value": item["previous_start_date"],
                "new_value": item["inferred_start_date"],
                "evidence": item["evidence"],
                "migration_version": MIGRATION_VERSION,
                "created_at": now,
            })
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
