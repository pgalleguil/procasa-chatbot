"""Restore genuine post-campaign customer replies from a WhatsApp chat export.

One lead, one campaign, one verified phone and one sender per execution.
DRY RUN by default. No WhatsApp API calls, AI calls, replayed webhooks or sends.

Use a genuine Android/iOS export WITHOUT MEDIA from the office WhatsApp.
The operator must compare the chat contact's phone to the CRM lead's phone.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bson import json_util
from dotenv import load_dotenv

if os.getenv("PRICE_DROP_ENV_FILE"):
    load_dotenv(Path(os.environ["PRICE_DROP_ENV_FILE"]).expanduser(), override=False)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatbot.phone_utils import normalize_phone_strict
from chatbot.price_drop_campaign_classifier import classify_price_drop_reply
from chatbot.storage import get_db
from config import Config

PILOT = "pilot_real_rebajas_20261109_v2"
CODES = {"16479", "6436", "6377"}
TZ = ZoneInfo("America/Santiago")
MAX_EXPORT_BYTES = 5_000_000
MAX_MESSAGES = 100
# Android: 12/11/2026, 11:32 - Ana: Hola
ANDROID = re.compile(
    r"^\u200e?(?P<day>\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}),?\s+"
    r"(?P<clock>\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?))?)\s+-\s+"
    r"(?P<sender>[^:]{1,150}):\s*(?P<text>.*)$", re.IGNORECASE,
)
# iOS: [12/11/2026, 11:32:05] Ana: Hola
IOS = re.compile(
    r"^\[(?P<day>\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}),?\s+"
    r"(?P<clock>\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?))?)\]\s+"
    r"(?P<sender>[^:]{1,150}):\s*(?P<text>.*)$", re.IGNORECASE,
)


class RecoveryBlocked(ValueError):
    pass


def _sender_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _when(day: str, clock: str) -> datetime:
    d = re.sub(r"[/.-]", "/", day)
    c = clock.replace("\u202f", " ").replace("\xa0", " ").upper().strip()
    c = re.sub(r"A\.?\s*M\.?", "AM", c)
    c = re.sub(r"P\.?\s*M\.?", "PM", c)
    c = re.sub(r"\s+", " ", c)
    for df in ("%d/%m/%Y", "%d/%m/%y"):
        for tf in ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p"):
            try:
                return datetime.strptime(d + " " + c, df + " " + tf).replace(tzinfo=TZ)
            except ValueError:
                pass
    raise RecoveryBlocked("export_date_format_unrecognized_check_day_month_order")


def _read_export(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > MAX_EXPORT_BYTES:
        raise RecoveryBlocked("export_missing_or_too_large")
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            entries = [x for x in archive.infolist() if
                       not x.is_dir() and x.filename.lower().endswith(".txt")]
            if len(entries) != 1 or entries[0].file_size > MAX_EXPORT_BYTES:
                raise RecoveryBlocked("zip_requires_exact_one_small_chat_txt")
            entry = entries[0]
            if entry.compress_size == 0 or entry.file_size / entry.compress_size > 200:
                raise RecoveryBlocked("zip_compression_ratio_suspicious")
            data = archive.read(entry)
    elif path.suffix.lower() == ".txt":
        data = path.read_bytes()
    else:
        raise RecoveryBlocked("export_must_be_whatsapp_txt_or_zip")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RecoveryBlocked("export_encoding_not_utf8") from exc


def parse_export(path: Path) -> list[dict]:
    messages: list[dict] = []
    for source_line in _read_export(path).splitlines():
        line = source_line.replace("\u200e", "").replace("\u202f", " ")
        match = IOS.match(line) or ANDROID.match(line)
        if match:
            msg = match.groupdict()
            stamp = _when(msg["day"], msg["clock"])
            messages.append({
                "at": stamp, "sender": msg["sender"].strip(),
                "text": msg["text"].strip(),
            })
        elif messages:
            messages[-1]["text"] += "\n" + source_line
    if not messages:
        raise RecoveryBlocked("export_has_no_recognized_chat_messages")
    return messages


def _datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            v = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _no_duplicate(message: dict, previous: list[dict]) -> bool:
    """Do not re-import a row already present via webhook or prior recovery."""
    when = message["at"].astimezone(timezone.utc)
    for old in previous:
        if old.get("role") not in {"user", "customer"}:
            continue
        old_at = _datetime(old.get("timestamp"))
        if old_at is not None and abs((old_at - when).total_seconds()) < 65:
            if str(old.get("content") or "").strip() == message["text"].strip():
                return False
        if old.get("recovery_id") == message["recovery_id"]:
            return False
    return True


def prepare_recovery(db, *, code: str, chat_phone: str,
                     customer_sender: str, export: Path,
                     now: datetime | None = None) -> dict:
    """Read-only deterministic plan. Requires explicit phone and sender."""
    if code not in CODES:
        raise RecoveryBlocked("outside_exact_three_pilot_properties")
    phone = normalize_phone_strict(chat_phone)
    if not phone or not customer_sender.strip():
        raise RecoveryBlocked("exact_chat_phone_and_sender_required")
    docs = list(db["leads"].find(
        {"price_drop_v2.campaigns.campaign_id": PILOT,
         "price_drop_v2.campaigns.property_code": code},
        {"_id": 1, "phone": 1, "contact_phone_normalized": 1,
         "messages": 1, "price_drop_v2.campaigns": 1},
    ).limit(3))
    if len(docs) != 1:
        raise RecoveryBlocked("pilot_lead_not_unique")
    lead = docs[0]
    stored_phone = normalize_phone_strict(lead.get("phone") or lead.get("contact_phone_normalized") or "")
    if stored_phone != phone:
        raise RecoveryBlocked("chat_phone_does_not_match_lead")
    campaigns = (lead.get("price_drop_v2") or {}).get("campaigns") or []
    positions = [i for i, c in enumerate(campaigns) if
                 c.get("campaign_id") == PILOT and str(c.get("property_code")) == code]
    if len(positions) != 1:
        raise RecoveryBlocked("pilot_campaign_not_unique")
    index = positions[0]
    campaign = campaigns[index]
    if campaign.get("status") not in {"SENT", "RESPONDED"}:
        raise RecoveryBlocked("pilot_campaign_not_in_sent_or_responded")
    sent_at = _datetime(campaign.get("sent_at"))
    if sent_at is None or not campaign.get("provider_message_id"):
        raise RecoveryBlocked("no_verified_real_campaign_send")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise RecoveryBlocked("now_must_be_timezone_aware")
    raw = parse_export(export)
    selected = []
    for m in raw:
        if _sender_key(m["sender"]) != _sender_key(customer_sender):
            continue
        at = m["at"].astimezone(timezone.utc)
        if not sent_at < at <= now.astimezone(timezone.utc) + timedelta(minutes=2):
            continue
        msg = m["text"].strip()
        if not msg or len(msg) > 4000 or re.search(
            r"(?i)^(?:<media omitted>|imagen omitida|multimedia omitido|archivo adjunto)",
            msg,
        ):
            continue
        fid = hashlib.sha256(
            (str(lead["_id"]) + "|" + PILOT + "|" + code + "|" +
             at.isoformat() + "|" + msg).encode("utf-8")
        ).hexdigest()
        selected.append({**m, "recovery_id": "whatsapp_export:" + fid})
    if len(selected) > MAX_MESSAGES:
        raise RecoveryBlocked("too_many_new_messages_manual_review")
    previous = lead.get("messages") or []
    unique = []
    for item in selected:
        if _no_duplicate(item, previous) and _no_duplicate(item, [
                {"role": "user", "timestamp": prev["at"].isoformat(),
                 "content": prev["text"], "recovery_id": prev["recovery_id"]}
                for prev in unique]):
            unique.append(item)
    return {"lead": lead, "index": index, "campaign": campaign, "code": code,
            "messages": unique, "source_count": len(raw),
            "selected_count": len(selected), "duplicate_count": len(selected)-len(unique)}


def apply_recovery(db, plan: dict, *, confirm: str, backup_root: Path) -> dict:
    """Atomic restore into leads only. Never calls WaSender or chatbot."""
    code = plan["code"]
    if confirm != f"RECUPERAR {code}":
        raise RecoveryBlocked("explicit_recovery_confirmation_required")
    messages = plan["messages"]
    if not messages:
        return {"status": "NO_NEW_MESSAGES", "property_code": code,
                "restored": 0, "duplicates": plan["duplicate_count"]}
    lead, campaign, i = plan["lead"], plan["campaign"], plan["index"]
    events, inserts = [], []
    for row in messages:
        stamp = row["at"].astimezone(TZ).isoformat()
        label = classify_price_drop_reply(row["text"]).as_dict()
        inserts.append({
            "role": "user", "content": row["text"], "timestamp": stamp,
            "lead_id": str(lead["_id"]), "actor_type": "customer",
            "message_id": row["recovery_id"],
            "recovery_id": row["recovery_id"],
            "recovery_source": "whatsapp_export_operator_verified",
            "campaign_id": PILOT, "property_code": code,
            "provider_message_id": None,  # original webhook ID is unavailable
        })
        events.append({
            "campaign_id": PILOT, "recovery_id": row["recovery_id"],
            "provider_message_id": None,
            "received_at": row["at"].astimezone(timezone.utc),
            "text": row["text"][:350], "classification": label,
            "source": "whatsapp_export_operator_verified",
            "requires_human_review": True,
        })
    last = events[-1]
    final_label = last["classification"]
    status = ("OPTED_OUT" if any(e["classification"]["label"] == "BAJA" for e in events)
              else "DECLINED" if final_label["label"] == "RECHAZADO"
              else "RESPONDED")
    field = f"price_drop_v2.campaigns.{i}"
    update = {
        "$push": {"messages": {"$each": inserts},
                  "price_drop_v2.reply_events": {"$each": events}},
        "$inc": {f"{field}.reply_count": len(inserts)},
        "$set": {
            f"{field}.status": status,
            f"{field}.last_customer_text": last["text"],
            f"{field}.last_customer_reply_at": last["received_at"],
            f"{field}.last_classification": final_label,
            f"{field}.recovered_from_export": True,
            f"{field}.recovery_review_status": "PENDING_HUMAN_REVIEW",
        },
    }
    if not campaign.get("first_response_at"):
        update["$set"][f"{field}.first_response_at"] = events[0]["received_at"]
    if status == "OPTED_OUT":
        update["$set"].update({
            "price_drop_v2.opted_out": True,
            "price_drop_v2.opted_out_at": last["received_at"],
            "price_drop_campaign_v1.opted_out": True,
            "conversation_status": "STOPPED_BY_CLIENT",
            "conversation_status_reason": final_label.get("reason", "customer_stop"),
        })
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / (f"pre_recovery_{code}_" +
                    datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    with backup_path.open("x", encoding="utf-8") as stream:
        stream.write(json_util.dumps(lead, ensure_ascii=False, indent=2))
    try:
        os.chmod(backup_path, 0o600)
    except OSError:
        pass
    match = {
        "_id": lead["_id"], f"{field}.campaign_id": PILOT,
        f"{field}.property_code": code,
        f"{field}.status": campaign["status"],
        "messages.recovery_id": {"$nin": [m["recovery_id"] for m in messages]},
    }
    result = db["leads"].update_one(match, update)
    if result.modified_count != 1:
        raise RecoveryBlocked("concurrent_change_or_duplicate_please_reaudit")
    return {"status": "RECOVERED_PENDING_HUMAN_REVIEW", "property_code": code,
            "restored": len(inserts), "duplicates": plan["duplicate_count"],
            "campaign_status": status, "backup": str(backup_path),
            "provider_sends": 0, "chatbot_replays": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--property-code", required=True, choices=sorted(CODES))
    parser.add_argument("--chat-export", type=Path, required=True)
    parser.add_argument("--chat-phone", required=True,
                        help="Exact contact number, visually verified in WhatsApp chat details")
    parser.add_argument("--customer-sender", required=True,
                        help="Exact client sender label as it appears in the export")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if not Config.MONGO_URI:
        raise RecoveryBlocked("MONGO_URI_not_configured_use_PRICE_DROP_ENV_FILE")
    if args.apply and not args.confirm:
        raise RecoveryBlocked("use_explicit_RECUPERAR_property_confirmation")
    plan = prepare_recovery(
        get_db(), code=args.property_code, chat_phone=args.chat_phone,
        customer_sender=args.customer_sender, export=args.chat_export,
    )
    report = {"status": "DRY_RUN", "property_code": plan["code"],
              "chat_messages_examined": plan["source_count"],
              "post_campaign_customer_messages": plan["selected_count"],
              "new_messages": len(plan["messages"]),
              "duplicates_or_already_recorded": plan["duplicate_count"],
              "messages": [{"at": m["at"].isoformat(), "text": m["text"],
                            "classification": classify_price_drop_reply(m["text"]).as_dict()}
                           for m in plan["messages"]],
              "provider_sends": 0}
    if args.apply:
        report = apply_recovery(
            get_db(), plan, confirm=args.confirm,
            backup_root=Path(__file__).resolve().parents[1] / ".pilot_recovery_backups",
        )
    print(json.dumps(report, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RecoveryBlocked as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)},
                         ensure_ascii=False))
        sys.exit(1)
