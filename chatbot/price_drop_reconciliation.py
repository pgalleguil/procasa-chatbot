"""Local write-ahead receipts for reconciling accepted WaSender messages.

The journal stores only lead/campaign/provider IDs and acceptance time. The
message body remains in the canonical lead campaign and leads.messages.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def default_journal_path() -> Path:
    configured = os.getenv("PRICE_DROP_RECONCILE_JOURNAL", "").strip()
    if configured:
        return Path(configured).expanduser()
    root = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return root / "PROCASA" / "price_drop_reconcile.json"


def _key(lead_id: str, campaign_id: str, provider_message_id: str) -> str:
    return "|".join((str(lead_id), str(campaign_id), str(provider_message_id)))


def _read(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise RuntimeError("reconciliation_journal_unreadable")
    if not isinstance(data, dict):
        raise RuntimeError("reconciliation_journal_invalid")
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _write(path: Path, records: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(records, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(raw, path)
    except Exception:
        try:
            os.unlink(raw)
        except OSError:
            pass
        raise


def record_accepted_receipt(path: str | Path, *, lead_id: str,
                            campaign_id: str, provider_message_id: str,
                            accepted_at: datetime) -> None:
    if not all((lead_id, campaign_id, provider_message_id)):
        raise ValueError("reconciliation_receipt_identity_required")
    if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
        raise ValueError("reconciliation_receipt_timestamp_requires_timezone")
    target = Path(path)
    records = _read(target)
    records[_key(lead_id, campaign_id, provider_message_id)] = {
        "lead_id": str(lead_id), "campaign_id": str(campaign_id),
        "provider_message_id": str(provider_message_id),
        "accepted_at": accepted_at.astimezone(timezone.utc).isoformat(),
    }
    _write(target, records)


def clear_accepted_receipt(path: str | Path, *, lead_id: str,
                           campaign_id: str, provider_message_id: str) -> None:
    target = Path(path)
    records = _read(target)
    records.pop(_key(lead_id, campaign_id, provider_message_id), None)
    _write(target, records)


def pending_receipts(path: str | Path) -> list[dict[str, Any]]:
    return list(_read(Path(path)).values())


def _campaign(lead: dict[str, Any], campaign_id: str) -> dict[str, Any] | None:
    return next((row for row in ((lead.get("price_drop_v2") or {}).get("campaigns") or [])
                 if row.get("campaign_id") == campaign_id), None)


def _message(campaign: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    stamp = datetime.fromisoformat(record["accepted_at"])
    currency = campaign.get("currency") or "UF"
    return {
        "role": "assistant",
        "content": campaign.get("outbound_message") or "",
        "provider_message_id": record["provider_message_id"],
        "delivery_status": "accepted",
        "timestamp": stamp.isoformat(),
        "metadata": {
            "tipo": "price_drop_outreach",
            "campaign_id": record["campaign_id"],
            "price_drop_event_id": str(campaign.get("event_id") or record["campaign_id"]),
            "property_code": str(campaign.get("property_code") or ""),
            "currency": currency,
            "provider_message_id": record["provider_message_id"],
            "provider_status": "accepted",
            "old_price_uf": campaign.get("old_price_uf"),
            "new_price_uf": campaign.get("new_price_uf"),
            "old_price_clp": campaign.get("old_price_clp"),
            "new_price_clp": campaign.get("new_price_clp"),
            "price_drop_at": campaign.get("price_drop_at"),
            "history_event_ids": campaign.get("history_event_ids") or [],
            "price_reference": campaign.get("price_reference"),
            "price_history_steps": campaign.get("price_history_steps") or [],
        },
    }


def reconcile_pending_receipts(db: Any, path: str | Path, *, now=None) -> dict[str, Any]:
    """Restore lead campaign and chat history from accepted provider receipts."""
    from .price_drop_campaign_real import _lead_id

    target = Path(path)
    records = pending_receipts(target)
    reconciled, unresolved = [], []
    stamp_now = now or datetime.now(timezone.utc)
    for receipt in records:
        try:
            lead = db["leads"].find_one({"_id": _lead_id(receipt["lead_id"])})
            campaign = _campaign(lead or {}, receipt["campaign_id"])
            accepted_at = datetime.fromisoformat(receipt["accepted_at"])
            if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
                raise ValueError("accepted_timestamp_invalid")
            if not lead or not campaign:
                raise ValueError("lead_or_campaign_missing")
            current_id = str(campaign.get("provider_message_id") or "")
            if current_id and current_id != receipt["provider_message_id"]:
                raise ValueError("provider_id_conflict")
            if campaign.get("status") not in {"SENDING", "SENT", "DELIVERY_UNKNOWN"}:
                raise ValueError("campaign_state_conflict")
            message = _message(campaign, receipt)
            fields = {
                "price_drop_v2.campaigns.$.status": "SENT",
                "price_drop_v2.campaigns.$.provider_message_id": receipt["provider_message_id"],
                "price_drop_v2.campaigns.$.provider_status": "accepted",
                "price_drop_v2.campaigns.$.sent_at": accepted_at,
                "price_drop_v2.campaigns.$.expires_at": accepted_at + timedelta(days=7),
            }
            has_echo = any(
                isinstance(item, dict)
                and item.get("provider_message_id") == receipt["provider_message_id"]
                for item in (lead.get("messages") or [])
            )
            if has_echo:
                result = db["leads"].update_one(
                    {"_id": lead["_id"], "price_drop_v2.campaigns.campaign_id": receipt["campaign_id"]},
                    {"$set": {
                        "price_drop_v2.campaigns.$.status": "SENT",
                        "price_drop_v2.campaigns.$.provider_message_id": receipt["provider_message_id"],
                        "price_drop_v2.campaigns.$.provider_status": "accepted",
                        "price_drop_v2.campaigns.$.sent_at": accepted_at,
                        "price_drop_v2.campaigns.$.expires_at": accepted_at + timedelta(days=7),
                    }},
                )
            else:
                result = db["leads"].update_one(
                    {"_id": lead["_id"],
                     "price_drop_v2.campaigns": {"$elemMatch": {
                         "campaign_id": receipt["campaign_id"],
                         "status": {"$in": ["SENDING", "SENT", "DELIVERY_UNKNOWN"]},
                         "provider_message_id": {"$in": [None, "", receipt["provider_message_id"]]},
                     }},
                     "messages.provider_message_id": {"$ne": receipt["provider_message_id"]}},
                    {"$set": fields, "$push": {"messages": message}},
                )
                if not result.modified_count:
                    # An outbound echo can arrive between the read and update.
                    echo = db["leads"].find_one(
                        {"_id": lead["_id"], "messages.provider_message_id":
                         receipt["provider_message_id"]}, {"_id": 1})
                    if not echo:
                        raise ValueError("reconciliation_write_conflict")
                    result = db["leads"].update_one(
                        {"_id": lead["_id"], "price_drop_v2.campaigns.campaign_id":
                         receipt["campaign_id"]},
                        {"$set": {
                            "price_drop_v2.campaigns.$.status": "SENT",
                            "price_drop_v2.campaigns.$.provider_message_id":
                                receipt["provider_message_id"],
                            "price_drop_v2.campaigns.$.provider_status": "accepted",
                            "price_drop_v2.campaigns.$.sent_at": accepted_at,
                            "price_drop_v2.campaigns.$.expires_at":
                                accepted_at + timedelta(days=7),
                        }},
                    )
            check = db["leads"].find_one(
                {"_id": lead["_id"],
                 "price_drop_v2.campaigns": {"$elemMatch": {
                     "campaign_id": receipt["campaign_id"], "status": "SENT",
                     "provider_message_id": receipt["provider_message_id"],
                 }},
                 "messages.provider_message_id": receipt["provider_message_id"]},
                {"_id": 1},
            )
            if not check:
                raise ValueError("reconciliation_readback_failed")
            clear_accepted_receipt(
                target, lead_id=receipt["lead_id"], campaign_id=receipt["campaign_id"],
                provider_message_id=receipt["provider_message_id"],
            )
            reconciled.append(receipt["campaign_id"])
        except Exception as exc:
            unresolved.append({
                "campaign_id": str(receipt.get("campaign_id") or ""),
                "reason": str(exc)[:80],
            })
    return {
        "pending": len(records), "reconciled": len(reconciled),
        "unresolved": unresolved,
    }
