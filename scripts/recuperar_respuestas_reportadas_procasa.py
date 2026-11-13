"""Restore two operator-reported replies from the 12 November price-drop pilot.

Run from project root:
  python scripts/recuperar_respuestas_reportadas_procasa.py
  python scripts/recuperar_respuestas_reportadas_procasa.py --apply --confirm RECUPERAR_REPORTE

The actual WhatsApp timestamps are unknown: do not pretend these are webhook
events or start a WhatsApp 24-hour window. No customer or executive sends.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId, json_util
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if os.getenv("PRICE_DROP_ENV_FILE"):
    load_dotenv(Path(os.environ["PRICE_DROP_ENV_FILE"]).expanduser(), override=False)
sys.path.insert(0, str(ROOT))

from chatbot.phone_utils import normalize_phone_strict
from chatbot.price_drop_campaign_classifier import classify_price_drop_reply
from chatbot.storage import get_db
from config import Config

PILOT = "pilot_real_rebajas_20261109_v2"
CASES = (
    ("6a1c84a30a43ac2651ad8947", "+56932654728", "16479",
     "buen dia, muchas gracias", "SIN_SENAL", "insufficient_intent"),
    ("6a592107592b78a8d88d50e3", "+56950922943", "6436",
     "muchas gracias pero ya encontramos un departamento. ya estoy por finalizar mi tramite de escritura y todo",
     "BAJA", "already_found_property"),
)


def plan_one(db, case):
    lead_id, phone, code, message, expected_label, expected_reason = case
    lead = db["leads"].find_one({"_id": ObjectId(lead_id), "phone": phone})
    if not lead or normalize_phone_strict(lead.get("phone")) != phone:
        raise ValueError("pilot_lead_phone_identity_mismatch:" + code)
    rows = [(index, row) for index, row in enumerate(
        (lead.get("price_drop_v2") or {}).get("campaigns") or [])
        if row.get("campaign_id") == PILOT and str(row.get("property_code")) == code]
    if len(rows) != 1:
        raise ValueError("pilot_campaign_not_unique:" + code)
    index, campaign = rows[0]
    if not campaign.get("provider_message_id") or not campaign.get("sent_at"):
        raise ValueError("verified_real_send_missing:" + code)
    classification = classify_price_drop_reply(message).as_dict()
    if (classification["label"], classification["reason"]) != (expected_label, expected_reason):
        raise ValueError("classifier_changed_review_before_recovery:" + code)
    identifier = "operator_report:" + hashlib.sha256(
        (PILOT + "|" + lead_id + "|" + code + "|" + message).encode("utf-8")
    ).hexdigest()
    duplicate = any(
        isinstance(m, dict) and m.get("role") in ("user", "customer")
        and (m.get("recovery_id") == identifier or
             str(m.get("content") or "").strip() == message)
        for m in lead.get("messages") or []
    )
    if duplicate or campaign.get("reply_count", 0) != 0 or campaign.get("status") != "SENT":
        return {"status": "REVIEW_OR_ALREADY_RECORDED", "code": code}
    return {"status": "READY", "case": case, "lead": lead,
            "campaign": campaign, "index": index,
            "classification": classification, "identifier": identifier}


def apply_one(db, item, backup_dir: Path):
    lead_id, phone, code, message, _, reason = item["case"]
    stamp = datetime.now(timezone.utc)
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / (
        "pre_operator_report_" + code + "_" + stamp.strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    with destination.open("x", encoding="utf-8") as output:
        output.write(json_util.dumps(item["lead"], ensure_ascii=False, indent=2))
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    inbound = {
        "role": "user", "content": message, "timestamp": None,
        "lead_id": lead_id, "actor_type": "customer",
        "message_id": item["identifier"], "recovery_id": item["identifier"],
        "recovery_source": "operator_report_not_whatsapp_export",
        "campaign_id": PILOT, "property_code": code,
        "provider_message_id": None, "recovered_at": stamp,
        "metadata": {
            "tipo": "price_drop_reply_manual_recovery",
            "original_timestamp_unknown": True,
            "operator_reported_date": "2026-11-12",
            "not_an_inbound_webhook": True,
        },
    }
    event = {
        "campaign_id": PILOT, "recovery_id": item["identifier"],
        "provider_message_id": None, "received_at": None, "recorded_at": stamp,
        "text": message, "classification": item["classification"],
        "source": "operator_report_not_whatsapp_export",
        "requires_human_review": True,
    }
    path = "price_drop_v2.campaigns." + str(item["index"])
    sets = {
        path + ".status": "RESPONDED",
        path + ".last_customer_text": message,
        path + ".last_classification": item["classification"],
        path + ".recovered_from_operator_report": True,
        path + ".recovery_recorded_at": stamp,
        path + ".recovery_review_status": "ORIGINAL_WHATSAPP_TIME_UNVERIFIED",
    }
    if reason == "already_found_property":
        # Completed search suppresses unsolicited campaigns. This is not an
        # explicit general-purpose STOP: customer-initiated reengagement may be handled.
        sets.update({
            "conversation_status": "STOPPED_BY_CLIENT",
            "conversation_status_reason": "already_found_property",
            "conversation_status_evidence": message,
            "price_drop_v2.marketing_suppression": {
                "active": True, "reason": "already_found_property",
                "scope": "UNSOLICITED_OUTBOUND_CAMPAIGNS",
                "source": "operator_report", "recorded_at": stamp,
            },
        })
    safe_filter = {
        "_id": ObjectId(lead_id), "phone": phone,
        "price_drop_v2.campaigns": {"$elemMatch": {
            "campaign_id": PILOT, "property_code": code,
            "status": "SENT", "reply_count": 0,
        }},
        "messages.recovery_id": {"$ne": item["identifier"]},
    }
    result = db["leads"].update_one(safe_filter, {
        "$push": {"messages": inbound, "price_drop_v2.reply_events": event},
        "$inc": {path + ".reply_count": 1},
        "$set": sets,
    })
    if result.modified_count != 1:
        raise RuntimeError("concurrent_change_or_duplicate:" + code)
    return {"code": code, "status": "RECOVERED", "last4": phone[-4:],
            "classification": item["classification"]["label"],
            "outbound_campaigns_suppressed": reason == "already_found_property",
            "backup": str(destination)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.apply and args.confirm != "RECUPERAR_REPORTE":
        parser.error("use --apply --confirm RECUPERAR_REPORTE")
    if not Config.MONGO_URI:
        parser.error("MONGO_URI_not_configured")
    db = get_db()
    plans = [plan_one(db, case) for case in CASES]
    report = [{"code": p["code"] if p["status"] != "READY" else p["case"][2],
               "status": p["status"]} for p in plans]
    if args.apply:
        if any(p["status"] != "READY" for p in plans):
            raise RuntimeError("one_or_more_replies_already_recorded_or_state_changed")
        report = [apply_one(db, p, ROOT / ".pilot_recovery_backups") for p in plans]
    print(json.dumps({"mode": "APPLY" if args.apply else "DRY_RUN",
                      "customer_sends": 0, "report": report},
                     ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
