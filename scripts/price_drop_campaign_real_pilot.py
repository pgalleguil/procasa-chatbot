"""Operator CLI for the explicitly approved small PROCASA price-drop pilot.

No recipients are discovered or selected automatically. No WhatsApp is sent
without BOTH '--send' and the two exact production environment gates.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatbot.price_drop_campaign_real import (
    CAMPAIGNS, CampaignBlocked, approve_campaign, campaign_report, campaign_worklist,
    halt_campaign, persist_plan, preview_plan, send_one_due,
)
from chatbot.storage import get_db



def run_approved_windows(db, campaign_id: str) -> dict:
    """Foreground, one-shot paced operator runner. Never runs at app startup."""
    from chatbot.price_drop_campaign_real import _is_enabled
    if not _is_enabled(campaign_id):
        raise CampaignBlocked("send_switch_or_approved_id_disabled")
    header = db[CAMPAIGNS].find_one({"_id": campaign_id, "status": "APPROVED"})
    if not header:
        raise CampaignBlocked("campaign_not_approved")
    now = datetime.now(timezone.utc)
    scheduled = header["start_at"]
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    if now < scheduled - timedelta(minutes=30):
        raise CampaignBlocked("watch_started_more_than_30m_before_first_slot")
    latest = scheduled + timedelta(
        minutes=(int(header["max_attempts"]) - 1) * int(header["gap_minutes"]) + 45
    )
    outcomes = []
    # No daemon or auto-retry. Finite runtime, bounded by last slot expiry.
    while datetime.now(timezone.utc) <= latest:
        fresh = db[CAMPAIGNS].find_one({"_id": campaign_id})
        if not fresh or fresh.get("status") != "APPROVED":
            return {"status": "HALTED", "outcomes": outcomes}
        if int(fresh.get("attempts_started") or 0) >= int(fresh["max_attempts"]):
            return {"status": "ATTEMPTS_COMPLETED", "outcomes": outcomes}
        if not _is_enabled(campaign_id):
            return {"status": "DISABLED", "outcomes": outcomes}
        current = datetime.now(timezone.utc)
        result = send_one_due(db, campaign_id, now=current)
        if result["status"] in {
            "SENT", "DELIVERY_UNKNOWN", "SKIPPED_PRICE_CHANGE",
            "SKIPPED_ELIGIBILITY", "EXPIRED_WINDOW",
        }:
            # Do not print phone numbers or message text.
            print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
            outcomes.append(result)
        if result["status"] in {"DISABLED", "NOT_APPROVED_OR_HALTED"}:
            return {"status": result["status"], "outcomes": outcomes}
        time.sleep(10)
    return {"status": "WINDOWS_EXPIRED", "outcomes": outcomes}

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PROCASA small-window pilot, default preview only")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare", action="store_true", help="Persist reviewed audience; no sends")
    action.add_argument("--approve", metavar="CAMPAIGN_ID", help="Separately approve a prepared campaign")
    action.add_argument("--send-due", metavar="CAMPAIGN_ID", help="Attempt at most one due recipient")
    action.add_argument("--watch", metavar="CAMPAIGN_ID", help="Run sequential due windows, in the foreground")
    action.add_argument("--halt", metavar="CAMPAIGN_ID", help="Immediate DB stop switch")
    action.add_argument("--report", metavar="CAMPAIGN_ID", help="Read-only outcomes")
    action.add_argument("--worklist", metavar="CAMPAIGN_ID", help="HOT-first list of leads pending commercial review")
    parser.add_argument("--file", type=Path, help="Local JSON with approved lead IDs")
    parser.add_argument("--operator", default="", help="Responsible reviewer/approver")
    parser.add_argument("--send", action="store_true", help="Mandatory explicit send confirmation")
    args = parser.parse_args(argv)
    if args.send and not (args.send_due or args.watch):
        parser.error("--send only works with --send-due or --watch")
    if (args.send_due or args.watch) and not args.send:
        parser.error("--send-due/--watch require --send, never silently trigger a send")
    try:
        db = get_db()
        if args.approve:
            output = approve_campaign(db, args.approve, operator=args.operator)
        elif args.halt:
            output = halt_campaign(db, args.halt, operator=args.operator)
        elif args.send_due:
            output = send_one_due(db, args.send_due)
        elif args.watch:
            output = run_approved_windows(db, args.watch)
        elif args.report:
            output = campaign_report(db, args.report)
        elif args.worklist:
            output = campaign_worklist(db, args.worklist)
        else:
            if not args.file:
                parser.error("--file required for preview/prepare")
            if not args.file.is_file():
                raise CampaignBlocked("plan_file_missing")
            payload = json.loads(args.file.read_text(encoding="utf-8"))
            if args.prepare:
                output = persist_plan(db, payload)
            else:
                raw = preview_plan(db, payload)
                output = {**raw, "approved": [
                    {k: v for k, v in row.items() if k != "_recipient"}
                    for row in raw["approved"]
                ]}
        print(json.dumps(output, ensure_ascii=False, default=str, indent=2))
        return 0
    except (CampaignBlocked, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
