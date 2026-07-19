"""Corrige de forma controlada la fecha de inicio de una membresía de Captación."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from captacion_workforce import MEMBERSHIP_COLLECTION, WORKFORCE_AUDIT_COLLECTION
from chatbot.storage import get_db


MIGRATION_VERSION = "membership_start_manual_correction_v1"


def backup_memberships(db) -> Path:
    target = Path("backups") / f"captacion_team_memberships_pre_{MIGRATION_VERSION}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(list(db[MEMBERSHIP_COLLECTION].find({})), ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--actor-user-id", default=f"migration:{MIGRATION_VERSION}")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    requested = date.fromisoformat(args.start_date).isoformat()
    db = get_db()
    membership = db[MEMBERSHIP_COLLECTION].find_one({"user_id": args.user_id})
    if not membership:
        raise RuntimeError("No existe la membresía indicada")
    previous = str(membership.get("start_date") or "")
    result = {
        "mode": "apply" if args.apply else "preview",
        "user_id": args.user_id,
        "previous_start_date": previous,
        "new_start_date": requested,
        "reason": args.reason,
        "changed": previous != requested,
    }
    if args.apply and previous != requested:
        result["backup"] = str(backup_memberships(db))
        now = datetime.now(timezone.utc)
        write = db[MEMBERSHIP_COLLECTION].update_one(
            {"user_id": args.user_id, "start_date": previous},
            {"$set": {
                "start_date": requested,
                "updated_at": now,
                "updated_by": args.actor_user_id,
                "start_date_correction_version": MIGRATION_VERSION,
            }},
        )
        if write.modified_count != 1:
            raise RuntimeError("La membresía cambió durante la corrección; no se aplicó")
        db[WORKFORCE_AUDIT_COLLECTION].insert_one({
            "event_type": "membership_start_date_corrected",
            "user_id": args.user_id,
            "actor_user_id": args.actor_user_id,
            "previous_value": previous,
            "new_value": requested,
            "reason": args.reason,
            "migration_version": MIGRATION_VERSION,
            "created_at": now,
        })
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
