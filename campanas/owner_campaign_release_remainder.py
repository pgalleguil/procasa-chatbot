"""Explicit, gated release of a prepared campaign remainder; never sends mail."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from pymongo import MongoClient

from config import Config
from campanas.owner_campaign_live_config import PRODUCTION_CAMPAIGN_ID
from campanas.owner_campaign_live_sender import normalize_property_code, hmac_compare


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PILOT_SELECTION = BASE_DIR / "reports" / "owner_campaign_sucre_wave1_pilot10_selection.csv"
PILOT_BATCH_ID = "pilot10_20260929"
EXPECTED_PILOT_COUNT = 10
EXPECTED_REMAINDER_COUNT = 270
EXPECTED_BATCH_SIZES = {
    "owner_price_sucre_wave1_batch_01": 50,
    "owner_price_sucre_wave1_batch_02": 50,
    "owner_price_sucre_wave1_batch_03": 50,
    "owner_price_sucre_wave1_batch_04": 50,
    "owner_price_sucre_wave1_batch_05": 50,
    "owner_price_sucre_wave1_batch_06": 20,
}


def load_expected_pilot(path: str | Path = DEFAULT_PILOT_SELECTION) -> tuple[str, set[str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("pilot_selection_empty")
    campaigns = {str(row.get("campaign_id") or "").strip() for row in rows}
    batches = {str(row.get("batch_id") or "").strip() for row in rows}
    codes = [normalize_property_code(row.get("property_code")) for row in rows]
    if len(campaigns) != 1 or "" in campaigns or batches != {PILOT_BATCH_ID}:
        raise ValueError("pilot_selection_identity_invalid")
    if len(codes) != EXPECTED_PILOT_COUNT or "" in codes or len(set(codes)) != EXPECTED_PILOT_COUNT:
        raise ValueError("pilot_selection_must_contain_exactly_10_unique_codes")
    return next(iter(campaigns)), set(codes)


def _addresses(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [part.strip() for part in values.split(",")]
    if not isinstance(values, (list, tuple, set)):
        return []
    return [str(value or "").strip().casefold() for value in values if str(value or "").strip()]


def _valid_email(value: Any) -> bool:
    normalized = str(value or "").strip()
    return bool(normalized and len(normalized) <= 254 and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized))


def _expected_cc(record: Mapping[str, Any]) -> list[str]:
    owner = str(record.get("owner_email") or "").strip().casefold()
    expected: list[str] = []
    for value in (record.get("boss_cc"), record.get("executive_email")):
        address = str(value or "").strip().casefold()
        if address and address != owner and address not in expected:
            expected.append(address)
    return expected


def evaluate_pilot(campaign_id: str, expected_codes: set[str], records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    campaign_records = [item for item in records if str(item.get("campaign_id") or "") == campaign_id]
    pilot = [item for item in campaign_records if str(item.get("campaign_stage") or "").upper() == "PILOT"]
    counts = Counter(normalize_property_code(item.get("property_code")) for item in pilot)
    actual_codes = {code for code in counts if code}
    missing = sorted(expected_codes - actual_codes)
    unexpected = sorted(actual_codes - expected_codes)
    duplicate_codes = sorted(code for code, count in counts.items() if count > 1)
    attempt_counts = {normalize_property_code(item.get("property_code")): len(item.get("send_attempts") or []) for item in pilot}
    attempted = sum(attempt_counts.values())
    sent = sum(str(item.get("send_status") or "").upper() == "SENT" for item in pilot)
    failed = sum(str(item.get("send_status") or "").upper() == "FAILED" for item in pilot)
    delivery_unknown = sum(str(item.get("send_status") or "").upper() == "DELIVERY_UNKNOWN" for item in pilot)
    smtp_accepted = 0
    to_correct = True
    cc_correct = True
    ledger_correct = not missing and not unexpected and not duplicate_codes
    duplicate_attempts = False
    for record in pilot:
        code = normalize_property_code(record.get("property_code"))
        attempts = record.get("send_attempts") or []
        status = str(record.get("send_status") or "").upper()
        if len(attempts) != 1:
            duplicate_attempts = True
        attempt = attempts[-1] if attempts else {}
        if str(attempt.get("smtp_status") or "").upper() == "SENT" and (attempt.get("message_id") or attempt.get("provider_message_id")):
            smtp_accepted += 1
        expected_to = str(record.get("owner_email") or "").strip().casefold()
        if (str(attempt.get("owner_email") or "").strip().casefold() != expected_to
                or not _valid_email(expected_to)):
            to_correct = False
        expected_cc = _expected_cc(record)
        attempt_cc = _addresses(attempt.get("final_cc_list"))
        if (not record.get("boss_cc") or not record.get("executive_email")
                or attempt_cc != expected_cc
                or any(not _valid_email(address) for address in attempt_cc)
                or len(attempt_cc) != len(set(attempt_cc))
                or expected_to in attempt_cc):
            cc_correct = False
        if (
            str(record.get("_id") or "") != f"{campaign_id}:{record.get('property_code')}"
            or str(record.get("campaign_id") or "") != campaign_id
            or code not in expected_codes
            or str(record.get("campaign_stage") or "").upper() != "PILOT"
            or str(record.get("batch_id") or "") != PILOT_BATCH_ID
            or status != "SENT"
            or str(attempt.get("batch_id") or "") != PILOT_BATCH_ID
            or str(attempt.get("smtp_status") or "").upper() != "SENT"
            or not attempt.get("message_id") and not attempt.get("provider_message_id")
        ):
            ledger_correct = False
    sent_outside_pilot = [
        normalize_property_code(item.get("property_code")) for item in campaign_records
        if str(item.get("send_status") or "").upper() == "SENT"
        and str(item.get("campaign_stage") or "").upper() != "PILOT"
    ]
    duplicates = bool(duplicate_codes or duplicate_attempts)
    ok = (
        len(expected_codes) == EXPECTED_PILOT_COUNT
        and len(pilot) == EXPECTED_PILOT_COUNT
        and actual_codes == expected_codes
        and not missing and not unexpected and not duplicates
        and attempted == EXPECTED_PILOT_COUNT
        and smtp_accepted == EXPECTED_PILOT_COUNT
        and sent == EXPECTED_PILOT_COUNT
        and failed == 0 and delivery_unknown == 0
        and not sent_outside_pilot
        and to_correct and cc_correct and ledger_correct
    )
    return {
        "allowed": ok,
        "expected": EXPECTED_PILOT_COUNT,
        "attempted": attempted,
        "smtp_accepted": smtp_accepted,
        "sent": sent,
        "failed": failed,
        "delivery_unknown": delivery_unknown,
        "duplicates": int(duplicates),
        "missing_codes": missing,
        "unexpected_codes": unexpected,
        "sent_outside_pilot": sent_outside_pilot,
        "to_correct": to_correct,
        "cc_correct": cc_correct,
        "ledger_correct": ledger_correct,
    }


def evaluate_release(campaign_id: str, expected_codes: set[str], records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pilot = evaluate_pilot(campaign_id, expected_codes, records)
    campaign_records = [item for item in records if str(item.get("campaign_id") or "") == campaign_id]
    remainder = [item for item in campaign_records if str(item.get("campaign_stage") or "").upper() == "REMAINDER"]
    batch_counts = Counter(str(item.get("batch_id") or "") for item in remainder)
    remainder_codes = [normalize_property_code(item.get("property_code")) for item in remainder]
    statuses_valid = all(str(item.get("send_status") or "").upper() in {"READY_PENDING_PILOT", "READY"} for item in remainder)
    batch_layout_valid = dict(batch_counts) == EXPECTED_BATCH_SIZES
    remainder_valid = (
        len(remainder) == EXPECTED_REMAINDER_COUNT
        and len(set(remainder_codes)) == EXPECTED_REMAINDER_COUNT
        and not (set(remainder_codes) & expected_codes)
        and statuses_valid and batch_layout_valid
    )
    return {
        "allowed": bool(pilot["allowed"] and remainder_valid),
        "campaign_id": campaign_id,
        "pilot": pilot,
        "remainder_total": len(remainder),
        "remainder_pending": sum(str(item.get("send_status") or "").upper() == "READY_PENDING_PILOT" for item in remainder),
        "remainder_ready": sum(str(item.get("send_status") or "").upper() == "READY" for item in remainder),
        "remainder_batches": dict(batch_counts),
        "remainder_layout_valid": remainder_valid,
        "remainder_statuses_valid": statuses_valid,
    }


def execute_release(db: Any, campaign_id: str, expected_codes: set[str], *, execute: bool = False) -> dict[str, Any]:
    ledger = db[Config.COLLECTION_CAMPANAS_LOG]
    records = list(ledger.find({"campaign_id": campaign_id}))
    result = evaluate_release(campaign_id, expected_codes, records)
    result["mode"] = "EXECUTE" if execute else "DRY_RUN"
    result["release"] = "ALLOWED" if result["allowed"] else "DENIED"
    result["modified_count"] = 0
    if not execute or not result["allowed"]:
        result["would_modify_count"] = result["remainder_pending"] if result["allowed"] else 0
        return result
    now = datetime.now(timezone.utc)
    update = ledger.update_many(
        {"campaign_id": campaign_id, "campaign_stage": "REMAINDER", "send_status": "READY_PENDING_PILOT"},
        {"$set": {"send_status": "READY", "released_at": now, "released_campaign_id": campaign_id}},
    )
    result["modified_count"] = int(update.modified_count)
    result["remainder_ready"] += int(update.modified_count)
    result["remainder_pending"] = max(0, result["remainder_pending"] - int(update.modified_count))
    result["would_modify_count"] = 0
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicitly release a prepared owner-campaign remainder; never sends")
    parser.add_argument("--campaign-id", default=PRODUCTION_CAMPAIGN_ID)
    parser.add_argument("--pilot-selection", default=str(DEFAULT_PILOT_SELECTION))
    parser.add_argument("--verify-pilot-only", action="store_true", help="Read-only pilot ledger check")
    parser.add_argument("--execute", action="store_true", help="Change pending remainder rows to READY")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        selected_campaign, codes = load_expected_pilot(args.pilot_selection)
        if args.campaign_id != selected_campaign or args.campaign_id != PRODUCTION_CAMPAIGN_ID:
            raise ValueError("campaign_id_does_not_match_approved_pilot_selection")
        if args.execute and not hmac_compare(args.confirm, f"RELEASE_OWNER_CAMPAIGN:{args.campaign_id}"):
            raise ValueError("execute_requires_campaign_confirmation")
        if args.verify_pilot_only and args.execute:
            raise ValueError("pilot_verify_cannot_execute_release")
        if not Config.MONGO_URI:
            raise ValueError("mongo_unavailable")
        client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=15000)
        try:
            db = client[Config.DB_NAME]
            records = list(db[Config.COLLECTION_CAMPANAS_LOG].find({"campaign_id": args.campaign_id}))
            if args.verify_pilot_only:
                pilot = evaluate_pilot(args.campaign_id, codes, records)
                result = {"mode": "VERIFY_PILOT_ONLY", "campaign_id": args.campaign_id, "pilot": pilot}
                result["status"] = "PILOT_PASS" if pilot["allowed"] else "PILOT_NOT_READY"
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                return 0 if pilot["allowed"] else 2
            result = execute_release(db, args.campaign_id, codes, execute=args.execute)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0 if result["allowed"] else 2
        finally:
            client.close()
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "DENIED", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
