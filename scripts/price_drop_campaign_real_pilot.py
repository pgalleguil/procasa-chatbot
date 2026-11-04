"""Operator CLI for the explicitly approved small PROCASA price-drop pilot.

No recipients are discovered or selected automatically. No WhatsApp is sent
without BOTH '--send' and the two exact production environment gates.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatbot.price_drop_campaign_real import (
    CampaignBlocked, approve_campaign, campaign_report, halt_campaign,
    persist_plan, preview_plan, send_one_due,
)
from chatbot.storage import get_db


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PROCASA small-window pilot, default preview only")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare", action="store_true", help="Persist reviewed audience; no sends")
    action.add_argument("--approve", metavar="CAMPAIGN_ID", help="Separately approve a prepared campaign")
    action.add_argument("--send-due", metavar="CAMPAIGN_ID", help="Attempt at most one due recipient")
    action.add_argument("--halt", metavar="CAMPAIGN_ID", help="Immediate DB stop switch")
    action.add_argument("--report", metavar="CAMPAIGN_ID", help="Read-only outcomes")
    parser.add_argument("--file", type=Path, help="Local JSON with approved lead IDs")
    parser.add_argument("--operator", default="", help="Responsible reviewer/approver")
    parser.add_argument("--send", action="store_true", help="Mandatory explicit send confirmation")
    args = parser.parse_args(argv)
    if args.send and not args.send_due:
        parser.error("--send is only valid with --send-due")
    if args.send_due and not args.send:
        parser.error("--send-due requires --send; never silently trigger a send")
    try:
        db = get_db()
        if args.approve:
            output = approve_campaign(db, args.approve, operator=args.operator)
        elif args.halt:
            output = halt_campaign(db, args.halt, operator=args.operator)
        elif args.send_due:
            output = send_one_due(db, args.send_due)
        elif args.report:
            output = campaign_report(db, args.report)
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
