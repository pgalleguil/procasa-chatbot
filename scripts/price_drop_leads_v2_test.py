"""Lead-only 6436 WhatsApp smoke test. No real recipients, dry-run by default.

Run on a trusted PROCASA host with DB/provider credentials. Never run
`--send` from CI or app startup. Preparation/approval are separate actions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatbot.storage import get_db
from chatbot.phone_utils import normalize_phone_strict
from chatbot.price_drop_leads_v2 import (
    PilotBlocked, preview_one, prepare_one, approve_one, send_one,
    TEST_PHONE_FLAG,
)

CODE = "6436"
OLD = 2790
NEW = 2690


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Single allowed-test-phone price-drop campaign; never bulk send"
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--prepare", action="store_true", help="Write PREPARED on the test lead; no WhatsApp")
    group.add_argument("--approve", action="store_true", help="Separate approval on the test lead; no WhatsApp")
    group.add_argument("--send", action="store_true", help="One provider attempt with extra runtime send gates")
    p.add_argument("--campaign-id", required=True, help="Unique stable test campaign id")
    p.add_argument("--operator", default="", help="Human approver")
    args = p.parse_args(argv)
    phone = normalize_phone_strict(os.getenv(TEST_PHONE_FLAG, ""))
    if not phone:
        print("BLOCKED: test_phone_not_configured")
        return 2
    db = get_db()
    leads = list(db["leads"].find({"phone": phone}, {"_id": 1, "phone": 1}).limit(2))
    if len(leads) != 1:
        print("BLOCKED: canonical_test_lead_ambiguous_or_missing")
        return 2
    lead_id = str(leads[0]["_id"])
    values = dict(lead_id=lead_id, phone=phone, campaign_id=args.campaign_id,
                  property_code=CODE, old_price_uf=OLD, new_price_uf=NEW,
                  test_mode=True)
    try:
        if args.approve:
            if not args.operator.strip():
                p.error("--operator required for --approve")
            result = approve_one(db, lead_id=lead_id, campaign_id=args.campaign_id,
                                 operator=args.operator)
        elif args.send:
            result = send_one(db, lead_id=lead_id, campaign_id=args.campaign_id)
        elif args.prepare:
            result = prepare_one(db, **values)
        else:
            row = preview_one(db, **values)
            result = {"status": "READY", "campaign_id": args.campaign_id,
                      "test_phone_last4": phone[-4:],
                      "message_preview": row["message"],
                      "test_prior_inquiry_simulated": True,
                      "writes": 0, "sends": 0}
    except PilotBlocked as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}))
        return 2
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
