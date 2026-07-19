"""Crea membresías explícitas de Captación desde el universo legado, una sola vez.

El criterio por comunas se usa exclusivamente para construir el plan inicial.
Después de aplicar, la aplicación consulta ``captacion_team_memberships``.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

from captacion_workforce import MEMBERSHIP_COLLECTION, upsert_membership
from chatbot.storage import get_db


def build_plan(db, start_date: str) -> list[dict]:
    users = db["usuarios"].find(
        {"is_active": True, "rol": "agente", "comunas_interes_norm": {"$exists": True, "$ne": []}},
        {"nombre": 1, "email": 1},
    ).sort("nombre", 1)
    return [
        {
            "user_id": str(user["_id"]),
            "name_snapshot": user.get("nombre") or user.get("email"),
            "enabled": True,
            "start_date": start_date,
            "end_date": None,
            "daily_target": 10,
            "workdays": [0, 1, 2, 3, 4],
            "supervisor_id": None,
            "timezone": "America/Santiago",
            "close_hour": 19,
            "migration_version": "workforce_v1",
        }
        for user in users
    ]


def backup_memberships(db) -> Path:
    target = Path("backups") / f"captacion_team_memberships_pre_v1_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = list(db[MEMBERSHIP_COLLECTION].find({}))
    target.write_text(json.dumps(rows, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--start-date", default=date.today().isoformat())
    parser.add_argument("--actor-user-id", default="migration:workforce_v1")
    args = parser.parse_args()
    db = get_db()
    plan = build_plan(db, args.start_date)
    print(json.dumps({"mode": "apply" if args.apply else "preview", "members": plan}, ensure_ascii=False, indent=2))
    if not args.apply:
        return
    backup = backup_memberships(db)
    for row in plan:
        saved = upsert_membership(db, row, args.actor_user_id)
        db[MEMBERSHIP_COLLECTION].update_one(
            {"user_id": row["user_id"]},
            {"$set": {"migration_version": row["migration_version"], "name_snapshot": row["name_snapshot"], "migrated_at": datetime.now(timezone.utc)}},
        )
    print(json.dumps({"applied": len(plan), "backup": str(backup)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
