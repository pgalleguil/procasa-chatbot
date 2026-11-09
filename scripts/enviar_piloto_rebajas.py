"""Manual CLI for PROCASA price-drop discovery and approved V2 campaigns."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

# This checkout intentionally has no copied secrets. Point PRICE_DROP_ENV_FILE
# at the operator's existing local .env before this module imports config.py.
from dotenv import load_dotenv
_configured_env_file = os.getenv("PRICE_DROP_ENV_FILE", "").strip()
if _configured_env_file:
    load_dotenv(dotenv_path=Path(_configured_env_file).expanduser(), override=False)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot import price_drop_leads_v2 as v2
from chatbot.price_drop_campaign_discovery import (
    CHILE_TZ, HISTORY_COLLECTION, LEADS_COLLECTION, PriceDropEvent,
    discover_price_drops, event_message, find_candidates_for_event,
    parse_aware_datetime, stable_event_id,
)
from chatbot.price_drop_reconciliation import (
    default_journal_path, pending_receipts, reconcile_pending_receipts,
)
from chatbot.price_drop_publication_check import verify_current_listing
from chatbot.wasender_usage import account_attempts_last_24h, WaSenderUsageUnavailable
from chatbot.price_drop_campaign_real import _lead_id

PILOT_CAMPAIGN_ID = "pilot_real_rebajas_20261109_v2"
PILOT_PROPERTY_CODES = {"6436", "6377", "16479"}
MAX_MESSAGES_PER_RUN = 50
MAX_DELAY_SECONDS = 3600
WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


class CliError(ValueError):
    pass


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (ValueError, TypeError) as exc:
        raise CliError(f"invalid_environment_value:{name}") from exc


def _parse_time(value: str) -> day_time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use HH:MM") from exc


def _weekdays(value: str) -> set[int]:
    tokens = {word.strip().upper() for word in value.split(",") if word.strip()}
    if not tokens or not tokens <= set(WEEKDAYS):
        raise CliError("allowed_weekdays_invalid")
    return {WEEKDAYS[item] for item in tokens}


def validate_limits(max_messages: int, min_delay: float, max_delay: float,
                    *, provider_plan: str = "trial", daily_cap: int = 50) -> None:
    if type(max_messages) is not int or not 1 <= max_messages <= MAX_MESSAGES_PER_RUN:
        raise CliError(f"max_messages_must_be_1_to_{MAX_MESSAGES_PER_RUN}")
    if min_delay < 0 or max_delay < 0:
        raise CliError("delays_must_not_be_negative")
    if min_delay > max_delay:
        raise CliError("max_delay_must_be_at_least_min_delay")
    if max_delay > MAX_DELAY_SECONDS:
        raise CliError(f"delay_must_not_exceed_{MAX_DELAY_SECONDS}_seconds")
    if provider_plan not in {"trial", "paid"}:
        raise CliError("wasender_plan_must_be_trial_or_paid")
    if provider_plan == "trial" and min_delay < 60:
        raise CliError("wasender_trial_requires_at_least_60_seconds_between_requests")
    if provider_plan == "paid" and min_delay < 5:
        raise CliError("wasender_protection_floor_is_5_seconds")
    if type(daily_cap) is not int or not 1 <= daily_cap <= 1000:
        raise CliError("daily_provider_cap_must_be_1_to_1000")
    if provider_plan == "trial" and daily_cap > 50:
        raise CliError("wasender_trial_daily_cap_must_not_exceed_50")


def _local_now(now: datetime, tz: ZoneInfo) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise CliError("timestamp_requires_timezone")
    return now.astimezone(tz)


def _within_hours(now: datetime, config: dict[str, Any]) -> bool:
    local = _local_now(now, config["timezone"])
    return (local.weekday() in config["weekdays"]
            and config["start"] <= local.timetz().replace(tzinfo=None) < config["end"])


def _provider_attempts_last_24h(*, now: datetime) -> dict[str, Any]:
    from config import Config
    try:
        return account_attempts_last_24h(
            now=now, base_url=str(Config.WASENDER_BASE_URL or ""),
            token=str(Config.WASENDER_TOKEN or ""),
            naive_timezone_name=os.getenv("PRICE_DROP_WASENDER_LOG_TIMEZONE", "UTC"),
        )
    except WaSenderUsageUnavailable as exc:
        raise CliError(str(exc)) from exc


def _entries(db: Any, campaign_id: str,
             statuses: tuple[str, ...] = ("APPROVED",),
             lead_allowlist: set[str] | None = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"price_drop_v2.campaigns": {"$elemMatch": {
        "campaign_id": campaign_id, "status": {"$in": list(statuses)},
    }}}
    if lead_allowlist is not None:
        query["_id"] = {"$in": [_lead_id(value) for value in lead_allowlist]}
    docs = db[LEADS_COLLECTION].find(
        query,
        {"_id": 1, "phone": 1, "contact_phone_normalized": 1, "messages": 1,
         "price_drop_v2.campaigns": 1, "price_drop_v2.opted_out": 1,
         "conversation_status": 1, "pipeline_stage": 1, "stage": 1},
    )
    output = []
    for lead in docs:
        matches = [item for item in ((lead.get("price_drop_v2") or {}).get("campaigns") or [])
                   if item.get("campaign_id") == campaign_id]
        if len(matches) > 1:
            raise CliError(f"ambiguous_campaign_rows:{str(lead.get('_id'))[-6:]}")
        if matches:
            output.append({"lead": lead, "campaign": matches[0]})
    return output


def _campaigns_by_status(db: Any, statuses: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    docs = db[LEADS_COLLECTION].find(
        {"price_drop_v2.campaigns.status": {"$in": list(statuses)}},
        {"_id": 1, "price_drop_v2.campaigns": 1},
    )
    result: dict[str, list[dict[str, Any]]] = {}
    for lead in docs:
        for campaign in ((lead.get("price_drop_v2") or {}).get("campaigns") or []):
            if campaign.get("status") in statuses and campaign.get("campaign_id"):
                result.setdefault(campaign["campaign_id"], []).append(
                    {"lead": lead, "campaign": campaign}
                )
    return result


def _phone_last4(lead: dict[str, Any]) -> str:
    return str(lead.get("contact_phone_normalized") or lead.get("phone") or "")[-4:]


def _status_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "campaign_id": rows[0]["campaign"].get("campaign_id") if rows else None,
        "recipient_count": len(rows),
        "statuses": dict(Counter(row["campaign"].get("status") or "UNKNOWN" for row in rows)),
        "rows": [{
            "property_code": row["campaign"].get("property_code"),
            "lead_last6": str(row["lead"].get("_id") or "")[-6:],
            "phone_last4": _phone_last4(row["lead"]),
            "status": row["campaign"].get("status"),
            "attempted_at": row["campaign"].get("attempted_at"),
            "sent_at": row["campaign"].get("sent_at"),
            "provider_status": row["campaign"].get("provider_status"),
            "reply_count": row["campaign"].get("reply_count", 0),
            "provider_id_present": bool(row["campaign"].get("provider_message_id")),
        } for row in rows],
    }


def _events(db: Any, property_code: str | None = None, *, since=None,
            currency: str | None = None, operation: str | None = None,
            limit: int | None = None, batch_size: int = 500) -> list[PriceDropEvent]:
    return discover_price_drops(
        db, since=since, property_code=property_code, currency=currency,
        operation=operation, limit=limit, batch_size=batch_size,
    )


def _history_steps(db: Any, event: PriceDropEvent,
                   since: datetime | None = None) -> list[PriceDropEvent]:
    # MongoDB returns naive datetimes as UTC unless the client enables tz_aware.
    if isinstance(since, datetime) and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    since_utc = parse_aware_datetime(since) if since is not None else None
    rows = db[HISTORY_COLLECTION].find(
        {"codigo": event.property_code, "campo": "precio_publicado"},
        {"_id": 1, "codigo": 1, "fecha": 1, "moneda_anterior": 1,
         "moneda_nueva": 1, "monto_anterior": 1, "monto_nuevo": 1,
         "valor_anterior": 1, "valor_nuevo": 1},
    ).sort("fecha", 1)
    result, seen = [], set()
    for item in rows:
        at = parse_aware_datetime(item.get("fecha"))
        before = item.get("monto_anterior", (item.get("valor_anterior") or {}).get("monto"))
        after = item.get("monto_nuevo", (item.get("valor_nuevo") or {}).get("monto"))
        c1 = str(item.get("moneda_anterior") or (item.get("valor_anterior") or {}).get("moneda") or "").upper()
        c2 = str(item.get("moneda_nueva") or (item.get("valor_nuevo") or {}).get("moneda") or "").upper()
        try:
            before, after = float(before), float(after)
        except (ValueError, TypeError):
            continue
        if (at is None or at > event.dropped_at or (since_utc and at <= since_utc)
                or c1 != event.currency or c2 != event.currency or not before > after > 0):
            continue
        step_id = stable_event_id(event.property_code, at, event.currency, before, after)
        if step_id in seen:
            continue
        seen.add(step_id)
        result.append(replace(
            event, event_id=step_id, campaign_id=step_id, dropped_at=at,
            old_price=int(before) if before.is_integer() else before,
            new_price=int(after) if after.is_integer() else after,
            difference=int(before-after) if (before-after).is_integer() else before-after,
            percent_reduction=(before-after)/before*100,
            history_id=str(item.get("_id") or ""),
        ))
    return result or [event]


def _display(event: PriceDropEvent, interested: int | None = None) -> str:
    from chatbot.price_drop_campaign_discovery import format_price
    tail = "" if interested is None else f" | consultas verificadas: {interested}"
    local = event.dropped_at.astimezone(CHILE_TZ)
    return (f"{event.property_code} | {event.property_type} | {event.comuna} | "
            f"{format_price(event.old_price,event.currency)} → "
            f"{format_price(event.new_price,event.currency)} (-{event.percent_reduction:.2f}%) | "
            f"{local:%d/%m/%Y %H:%M %Z}{tail}")


def _pilot_status(db: Any) -> dict[str, Any]:
    rows = _entries(db, PILOT_CAMPAIGN_ID, (
        "PREPARED", "APPROVED", "SENDING", "SENT", "DELIVERY_UNKNOWN",
        "RESPONDED", "DECLINED", "OPTED_OUT", "BLOCKED",
    ))
    codes = {str(row["campaign"].get("property_code") or "") for row in rows}
    history_count = sum(
        1 for row in rows for message in row["lead"].get("messages") or []
        if isinstance(message, dict)
        and (message.get("metadata") or {}).get("campaign_id") == PILOT_CAMPAIGN_ID
    )
    return {**_status_report(rows), "pilot_property_set_complete": codes == PILOT_PROPERTY_CODES,
            "messages_in_history": history_count}


def _backup_leads(db: Any, ids: list[Any]) -> Path:
    stamp = datetime.now(timezone.utc)
    docs = [db[LEADS_COLLECTION].find_one({"_id": value}) for value in ids]
    import bson.json_util
    target = default_journal_path().parent / "backups"
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"price_drop_leads_{stamp:%Y%m%dT%H%M%SZ}.json"
    path.write_text(bson.json_util.dumps([doc for doc in docs if doc],
                                        ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _verify_current_links(db: Any, expected: dict[str, dict[str, Any]],
                          now: datetime) -> dict[str, dict[str, Any]]:
    checks = {}
    for code, row in expected.items():
        checks[code] = verify_current_listing(
            db, property_code=code,
            currency=str(row.get("currency") or ""),
            expected_price=row.get("price"), expected_url=str(row.get("url") or ""),
            now=now,
        ).as_dict()
    return checks


def _expected_publication_from_campaign(campaign: dict[str, Any]) -> dict[str, Any]:
    currency = str(campaign.get("currency") or "UF").upper()
    price = campaign.get("new_price_clp") if currency == "CLP" else campaign.get("new_price_uf")
    match = re.search(r"(?im)^Publicaci[oó]n:\s*(https://\S+)",
                      str(campaign.get("outbound_message") or ""))
    url = match.group(1).rstrip(".,);]") if match else ""
    return {"currency": currency, "price": price, "url": url}


def _publication_result_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "as_dict"):
        return result.as_dict()
    if isinstance(result, dict):
        return result
    raise CliError("publication_verifier_result_invalid")
@contextlib.contextmanager
def _execution_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _provider_ready() -> bool:
    from config import Config
    return bool(Config.WASENDER_TOKEN
                and str(Config.WASENDER_BASE_URL or "").startswith("https://"))


def _message_window_blocker(entry: dict[str, Any], now: datetime) -> str | None:
    mode = os.getenv("PRICE_DROP_WHATSAPP_CHANNEL_MODE", "wasenderapi").strip().lower()
    if mode == "wasenderapi":
        # WaSender's configured session endpoint accepts plain text and the
        # current integration has no official Cloud API template/window path.
        # Contact authorization and unsubscribe checks remain separate gates.
        return None
    if mode != "official":
        return "whatsapp_channel_mode_invalid"
    inbound = []
    for message in entry["lead"].get("messages") or []:
        if not isinstance(message, dict) or message.get("role") not in {"user", "customer"}:
            continue
        raw_stamp = message.get("timestamp")
        if isinstance(raw_stamp, datetime) and raw_stamp.tzinfo is None:
            stamp = raw_stamp.replace(tzinfo=timezone.utc)
        else:
            stamp = parse_aware_datetime(raw_stamp)
        if stamp is not None:
            inbound.append(stamp)
    if not inbound:
        return "whatsapp_24_hour_window_expired_template_required"
    last_customer = max(inbound)
    if now - last_customer > timedelta(hours=24):
        return "whatsapp_24_hour_window_expired_template_required"
    return None


def execute_campaign(
    db: Any, *, campaign_id: str, max_messages: int = 20,
    min_delay_seconds: float = 300, max_delay_seconds: float = 600,
    start_time: day_time = day_time(9), end_time: day_time = day_time(19),
    allowed_weekdays: str = "MON,TUE,WED,THU,FRI",
    timezone_name: str = "America/Santiago", provider_plan: str = "trial",
    daily_provider_cap: int = 50, live_links_verified: bool = False,
    confirm: Callable[[str], str] = input, sleep_fn: Callable[[float], None] = time.sleep,
    sender: Callable[[str, str], dict] | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    provider_ready: Callable[[], bool] = _provider_ready,
    usage_reader: Callable[[datetime], dict[str, Any] | int] | None = None,
    journal_path: str | Path | None = None,
    rng: Callable[[float, float], float] = random.uniform,
    allowed_property_codes: set[str] | None = None,
    link_verifier: Callable[[str, datetime], bool] | None = None,
    lead_allowlist: set[str] | None = None,
    pause_after_last: bool = False,
    expected_messages: dict[str, str] | None = None,
) -> dict[str, Any]:
    validate_limits(max_messages, min_delay_seconds, max_delay_seconds,
                    provider_plan=provider_plan, daily_cap=daily_provider_cap)
    if start_time >= end_time:
        raise CliError("business_hours_start_must_precede_end")
    tz = ZoneInfo(timezone_name)
    config = {"timezone": tz, "weekdays": _weekdays(allowed_weekdays),
              "start": start_time, "end": end_time}
    if not live_links_verified or link_verifier is None:
        return {"status": "BLOCKED", "reason": "current_publication_verifier_required", "attempts": 0}
    if os.getenv("RENDER", "").strip().lower() == "true" or os.getenv("RENDER_SERVICE_ID"):
        return {"status": "BLOCKED", "reason": "manual_cli_disabled_on_render", "attempts": 0}
    if not provider_ready():
        return {"status": "BLOCKED", "reason": "wasender_provider_not_configured", "attempts": 0}
    if not _truthy(os.getenv(v2.WHATSAPP_CONTACT_REVIEWED_FLAG)):
        return {"status": "BLOCKED", "reason": "whatsapp_contact_policy_review_required", "attempts": 0}
    if (os.getenv(v2.SEND_FLAG, "").strip().lower() not in {"1", "true", "yes"}
            or os.getenv(v2.APPROVED_ID_FLAG) != campaign_id):
        return {"status": "BLOCKED", "reason": "v2_campaign_runtime_gate_disabled", "attempts": 0}
    if os.getenv(v2.CONTEXT_FLAG, "").strip().lower() not in {"1", "true", "yes"}:
        return {"status": "BLOCKED", "reason": "campaign_context_disabled", "attempts": 0}
    campaign_gate = (v2.REAL_PILOT_ENABLED_FLAG if campaign_id == PILOT_CAMPAIGN_ID
                     else v2.REAL_CAMPAIGNS_ENABLED_FLAG)
    if os.getenv(campaign_gate, "").strip().lower() not in {"1", "true", "yes"}:
        return {"status": "BLOCKED", "reason": "real_campaign_runtime_gate_disabled", "attempts": 0}
    channel = os.getenv("PRICE_DROP_WHATSAPP_CHANNEL_MODE", "wasenderapi").strip().lower()
    if channel not in {"wasenderapi", "official"}:
        return {"status": "BLOCKED", "reason": "whatsapp_channel_mode_invalid", "attempts": 0}
    if channel == "official":
        return {"status": "BLOCKED", "reason": "official_channel_sender_not_implemented", "attempts": 0}
    now = now_fn()
    if not _within_hours(now, config):
        return {"status": "BLOCKED", "reason": "outside_business_hours", "attempts": 0}

    journal = Path(journal_path) if journal_path else default_journal_path()
    reconciliation = reconcile_pending_receipts(db, journal, now=now)
    if reconciliation["unresolved"]:
        return {"status": "BLOCKED", "reason": "accepted_message_reconciliation_required",
                "reconciliation": reconciliation, "attempts": 0}
    if pending_receipts(journal):
        return {"status": "BLOCKED", "reason": "reconciliation_journal_not_empty", "attempts": 0}
    uncertain = _entries(db, campaign_id, ("SENDING", "DELIVERY_UNKNOWN"), lead_allowlist)
    if uncertain:
        return {"status": "BLOCKED", "reason": "uncertain_delivery_requires_reconciliation",
                "uncertain_count": len(uncertain), "attempts": 0}

    entries = _entries(db, campaign_id, ("APPROVED",), lead_allowlist)
    if not entries:
        return {"status": "NO_APPROVED_RECIPIENTS", "attempts": 0, "pending": 0}
    if expected_messages is not None:
        current_snapshot = {str(row["lead"]["_id"]): row["campaign"].get("outbound_message")
                            for row in entries}
        if current_snapshot != expected_messages:
            return {"status": "BLOCKED", "reason": "approved_messages_changed_after_final_preview",
                    "attempts": 0, "pending": len(entries)}
    if any(not row["campaign"].get("outbound_message") for row in entries):
        return {"status": "BLOCKED", "reason": "approved_message_missing", "attempts": 0}
    if allowed_property_codes is not None and any(
            str(row["campaign"].get("property_code") or "") not in allowed_property_codes
            for row in entries):
        return {"status": "BLOCKED", "reason": "property_outside_campaign_allowlist", "attempts": 0}
    if any(row["campaign"].get("whatsapp_contact_reviewed") is not True
           or not str(row["campaign"].get("whatsapp_contact_evidence") or "").strip()
           for row in entries):
        return {"status": "BLOCKED", "reason": "whatsapp_contact_authorization_review_required",
                "attempts": 0}
    for entry in entries:
        reason = _message_window_blocker(entry, now)
        if reason:
            return {"status": "BLOCKED", "reason": reason, "attempts": 0}

    print("Mensajes exactos aprobados para esta ejecución:")
    for index, row in enumerate(entries, start=1):
        print(f"\n[{index}] propiedad {row['campaign'].get('property_code')} | "
              f"teléfono ***{_phone_last4(row['lead'])}")
        print(row["campaign"]["outbound_message"])
    expected = f"ENVIAR {campaign_id}"
    if confirm(f"\nEscribe exactamente {expected} para continuar: ").strip() != expected:
        return {"status": "CANCELLED", "reason": "manual_confirmation_not_matched",
                "attempts": 0, "pending": len(entries)}

    now = now_fn()
    if not _within_hours(now, config):
        return {"status": "BLOCKED", "reason": "outside_business_hours", "attempts": 0}
    journal_lock = journal.with_suffix(".lock")
    results, attempts, daily_before, run_limit = [], 0, 0, 0
    read_usage = usage_reader or (lambda stamp: _provider_attempts_last_24h(now=stamp))

    def usage_count(stamp: datetime) -> int:
        result = read_usage(stamp)
        if isinstance(result, dict):
            return int(result.get("attempts_last_24h", result.get("attempts", -1)))
        return int(result)

    with _execution_lock(journal_lock):
        # Re-read after the operator confirms and while holding the local lock.
        current_entries = _entries(db, campaign_id, ("APPROVED",), lead_allowlist)
        initial = {str(row["lead"]["_id"]): row["campaign"].get("outbound_message")
                   for row in entries}
        current = {str(row["lead"]["_id"]): row["campaign"].get("outbound_message")
                   for row in current_entries}
        if current != initial:
            return {"status": "BLOCKED", "reason": "campaign_changed_after_preview", "attempts": 0}
        entries = current_entries
        try:
            daily_before = usage_count(now_fn())
        except Exception as exc:
            return {"status": "BLOCKED", "reason": str(exc)[:120], "attempts": 0,
                    "pending": len(entries)}
        if daily_before < 0:
            return {"status": "BLOCKED", "reason": "provider_usage_count_invalid",
                    "attempts": 0, "pending": len(entries)}
        run_limit = min(max_messages, max(daily_provider_cap - daily_before, 0))
        if run_limit == 0:
            return {"status": "BLOCKED", "reason": "daily_provider_limit_reached",
                    "attempts": 0, "pending": len(entries)}
        for index, entry in enumerate(entries):
            if attempts >= max_messages:
                break
            now = now_fn()
            try:
                daily_current = max(usage_count(now), daily_before + attempts)
            except Exception as exc:
                return {"status": "BLOCKED_PROVIDER_USAGE_UNAVAILABLE",
                        "reason": str(exc)[:120], "attempts": attempts,
                        "pending": max(len(entries)-len(results), 0), "results": results}
            if daily_current >= daily_provider_cap:
                break
            if link_verifier is not None:
                verification = link_verifier(
                    str(entry["campaign"].get("property_code") or ""), now,
                )
                if isinstance(verification, dict):
                    is_sendable = bool(verification.get("sendable"))
                    verification_status = str(verification.get("status") or "unknown")
                else:
                    is_sendable = bool(verification)
                    verification_status = "current_published_link_not_verified"
                if not is_sendable:
                    results.append({"status": "BLOCKED", "reason": verification_status,
                                    "lead_last6": str(entry["lead"].get("_id"))[-6:]})
                    continue
            reason = _message_window_blocker(entry, now)
            if reason:
                results.append({"status": "BLOCKED", "reason": reason})
                continue
            outcome = v2.send_one(
                db, lead_id=str(entry["lead"]["_id"]), campaign_id=campaign_id,
                sender=sender, now=now, recovery_journal_path=journal,
            )
            status = str(outcome.get("status") or "UNKNOWN")
            results.append({
                "status": status, "lead_last6": str(entry["lead"].get("_id"))[-6:],
                "provider_id_present": bool(outcome.get("provider_message_id")),
            })
            if status in {"SENT", "DELIVERY_UNKNOWN", "HISTORY_RECONCILIATION_REQUIRED"}:
                attempts += 1
            if status in {"DELIVERY_UNKNOWN", "HISTORY_RECONCILIATION_REQUIRED",
                          "SENDING", "ALREADY_ATTEMPTED"}:
                return {"status": "STOPPED_FOR_RECONCILIATION", "attempts": attempts,
                        "pending": max(len(entries)-len(results), 0), "results": results}
            if status == "SENT":
                remaining = len(entries) - index - 1
                if remaining == 0 and pause_after_last:
                    remaining = 1
                if remaining and attempts < run_limit:
                    pause = rng(min_delay_seconds, max_delay_seconds)
                    wake = now_fn() + timedelta(seconds=pause)
                    if not _within_hours(wake, config):
                        return {
                            "status": "STOPPED_OUTSIDE_BUSINESS_HOURS", "attempts": attempts,
                            "pending": remaining, "last_result": status,
                            "selected_wait_seconds": pause,
                            "next_attempt_local": _local_now(wake, tz).isoformat(),
                            "results": results,
                        }
                    print(f"Procesados: {attempts} | cupo restante: {run_limit-attempts} | "
                          f"último resultado: {status} | espera: {pause:.0f} s | "
                          f"próximo intento: {_local_now(wake, tz):%d/%m/%Y %H:%M:%S %Z}")
                    try:
                        sleep_fn(pause)
                    except KeyboardInterrupt:
                        return {"status": "INTERRUPTED_DURING_PAUSE", "attempts": attempts,
                                "pending": remaining, "results": results}
        pending = len(_entries(db, campaign_id, ("APPROVED",), lead_allowlist))
        try:
            daily_after = max(usage_count(now_fn()), daily_before + attempts)
        except Exception:
            daily_after = daily_before + attempts
    final_status = "COMPLETED"
    if pending and (attempts >= max_messages or daily_after >= daily_provider_cap):
        final_status = "LIMIT_REACHED"
    elif pending and any(row.get("status") == "BLOCKED" for row in results):
        final_status = "BLOCKED_RECIPIENTS_PENDING"
    return {
        "status": final_status,
        "attempts": attempts, "run_limit": run_limit,
        "daily_attempts_before": daily_before, "daily_provider_cap": daily_provider_cap,
        "daily_attempts_after": daily_after, "pending": pending, "results": results,
    }


def prepare_selected(db: Any, *, event: PriceDropEvent, lead_ids: list[str],
                     operator: str, contact_basis_reviewed: bool,
                     whatsapp_contact_reviewed: bool,
                     whatsapp_contact_evidence: str | dict[str, str]) -> dict[str, Any]:
    if not operator.strip():
        raise CliError("operator_required")
    if not contact_basis_reviewed:
        raise CliError("contact_basis_must_be_reviewed")
    if not whatsapp_contact_reviewed:
        raise CliError("whatsapp_contact_authorization_review_required")
    if not lead_ids:
        raise CliError("at_least_one_lead_id_required")
    candidates = {row["lead_id"]: row for row in find_candidates_for_event(db, event)}
    if isinstance(whatsapp_contact_evidence, dict):
        evidence_by_lead = {str(key): str(value or "").strip()
                            for key, value in whatsapp_contact_evidence.items()}
        if any(len(evidence_by_lead.get(str(value), "")) < 8 for value in lead_ids):
            raise CliError("whatsapp_contact_authorization_evidence_required_per_lead")
    else:
        evidence = str(whatsapp_contact_evidence or "").strip()
        if len(evidence) < 8:
            raise CliError("whatsapp_contact_authorization_review_required")
        evidence_by_lead = {str(value): evidence for value in lead_ids}
    output = []
    for lead_id in dict.fromkeys(lead_ids):
        candidate = candidates.get(str(lead_id))
        if not candidate or not candidate["eligible"]:
            raise CliError(f"selected_lead_not_eligible:{str(lead_id)[-6:]}")
        lead = db[LEADS_COLLECTION].find_one({"_id": _lead_id(lead_id)}) or {}
        history = _history_steps(db, event, candidate["last_notice"].get("sent_at")
                                 if candidate["last_notice"] else None)
        body = event_message(event, last_notice=candidate["last_notice"], history=history)
        notified_price = (candidate["last_notice"] or {}).get("price")
        price_reference = notified_price if notified_price is not None else history[0].old_price
        history_steps = [{
            "event_id": row.event_id, "dropped_at": row.dropped_at.isoformat(),
            "currency": row.currency, "old_price": row.old_price,
            "new_price": row.new_price,
        } for row in history]
        data = {
            "lead_id": str(lead_id),
            "phone": lead.get("contact_phone_normalized") or lead.get("phone") or "",
            "campaign_id": event.campaign_id, "property_code": event.property_code,
            "currency": event.currency, "test_mode": False,
            "old_price_uf": event.old_price if event.currency == "UF" else None,
            "new_price_uf": event.new_price if event.currency == "UF" else None,
            "old_price_clp": event.old_price if event.currency == "CLP" else None,
            "new_price_clp": event.new_price if event.currency == "CLP" else None,
            "contact_basis_reviewed": True,
            "contact_basis_type": v2.DIRECT_RESPONSE_BASIS_TYPE,
            "contact_basis": (
                f"Consulta propiedad verificada {candidate['inquiry_at']}; "
                f"referencia {candidate['inquiry_reference'] or event.property_code}"
            ),
            "event_id": event.event_id,
            "history_event_ids": [item.event_id for item in history],
            "price_reference": price_reference,
            "price_history_steps": history_steps,
            "outbound_message_override": body,
        }
        prepared = v2.prepare_one(db, **data)
        v2.review_whatsapp_contact(
            db, lead_id=str(lead_id), campaign_id=event.campaign_id,
            operator=operator, evidence=evidence_by_lead[str(lead_id)],
        )
        output.append(prepared)
    return {"status": "PREPARED", "campaign_id": event.campaign_id,
            "prepared_count": len(output),
            "lead_last6": [str(row["lead_id"])[-6:] for row in output],
            "whatsapp_review_evidence_present": True}


def approve_selected(db: Any, *, campaign_id: str, lead_ids: list[str],
                     operator: str) -> dict[str, Any]:
    if not operator.strip() or not lead_ids:
        raise CliError("operator_and_lead_ids_required")
    output = [v2.approve_one(db, lead_id=lead_id, campaign_id=campaign_id,
                              operator=operator)
              for lead_id in dict.fromkeys(lead_ids)]
    return {"status": "APPROVED", "campaign_id": campaign_id,
            "approved_count": len(output)}


def review_selected_whatsapp_contact(db: Any, *, campaign_id: str,
                                     lead_ids: list[str], operator: str,
                                     evidence: str) -> dict[str, Any]:
    if not operator.strip() or len(evidence.strip()) < 8 or not lead_ids:
        raise CliError("operator_leads_and_whatsapp_evidence_required")
    output = [v2.review_whatsapp_contact(
        db, lead_id=lead_id, campaign_id=campaign_id,
        operator=operator, evidence=evidence,
    ) for lead_id in dict.fromkeys(lead_ids)]
    return {"status": "WHATSAPP_CONTACT_REVIEWED", "campaign_id": campaign_id,
            "reviewed_count": len(output)}


def _resolve_event(db: Any, event_id: str, *, since=None, property_code=None) -> PriceDropEvent:
    matches = [event for event in _events(db, property_code, since=since)
               if event.event_id == event_id and (since is None or event.dropped_at >= since)]
    if len(matches) != 1:
        raise CliError("current_price_drop_event_not_found_or_ambiguous")
    return matches[0]


def _print_events(db: Any, *, detailed: bool, since=None,
                  property_code: str | None = None, currency: str | None = None,
                  operation: str | None = None, limit: int | None = None,
                  batch_size: int = 500) -> int:
    events = _events(db, property_code, since=since, currency=currency,
                     operation=operation, limit=limit, batch_size=batch_size)
    if not events:
        print("No hay rebajas vigentes que cumplan los filtros.")
        return 0
    print("Propiedad | Tipo | Comuna | Precio anterior → vigente | Cambio | Fecha (Chile) | Interesados")
    for event in events:
        candidates = find_candidates_for_event(db, event)
        eligible = sum(1 for candidate in candidates if candidate["eligible"])
        print(_display(event, eligible))
        if not detailed:
            continue
        print(f"  evento={event.event_id} | operación={event.operation} | disponible={event.available} | "
              f"publicación_vigente={event.published} | historial={event.history_id}")
        history = _history_steps(db, event)
        sample = next((row for row in candidates if row["eligible"]), None)
        if sample:
            print("  Mensaje de ejemplo:")
            print("  " + event_message(event, history=history).replace("\n", "\n  "))
        for row in candidates:
            history_rows = "; ".join(
                f"{item.get('status')}@{item.get('sent_at')}"
                for item in row["campaign_history"]
            ) or "sin campañas previas"
            inquiry = row["inquiry_at"].astimezone(CHILE_TZ).isoformat() if row["inquiry_at"] else "N/A"
            print(
                f"  lead …{row['lead_id'][-6:]} | teléfono ***{row['phone_last4']} | "
                f"{row['temperature']} | consulta={inquiry} | "
                f"evidencia={row['inquiry_reference'] or 'N/A'} | "
                f"historial={history_rows} | último aviso={row['last_notice'] or 'N/A'} | "
                f"elegible={row['eligible']} ({row['reason']})"
            )
    print(f"Eventos vigentes: {len(events)}")
    return 0


def _menu_selection(raw: str, count: int) -> list[int]:
    value = str(raw or "").strip().lower()
    if value == "all":
        return list(range(1, count + 1))
    try:
        selected = list(dict.fromkeys(int(token.strip()) for token in value.split(",")
                                      if token.strip()))
    except ValueError as exc:
        raise CliError("menu_selection_must_be_comma_separated_numbers_or_all") from exc
    if not selected or any(number < 1 or number > count for number in selected):
        raise CliError(f"menu_selection_must_be_1_to_{count}")
    return selected


def _preview_message(db: Any, event: PriceDropEvent,
                     candidate: dict[str, Any]) -> dict[str, Any]:
    history = _history_steps(db, event, candidate["last_notice"].get("sent_at")
                             if candidate["last_notice"] else None)
    return {
        "event": event,
        "candidate": candidate,
        "message": event_message(
            event, last_notice=candidate["last_notice"], history=history,
        ),
    }


@contextlib.contextmanager
def _temporary_environment(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def interactive_workflow(
    db: Any, *, input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    sender: Callable[[str, str], dict] | None = None,
    usage_reader: Callable[[datetime], dict[str, Any] | int] | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    since: datetime | None = None, property_code: str | None = None,
    currency: str | None = None, operation: str | None = None,
    publication_verifier: Callable[..., Any] = verify_current_listing,
    provider_ready: Callable[[], bool] = _provider_ready,
    backup_fn: Callable[[Any, list[Any]], Path] = _backup_leads,
    sleep_fn: Callable[[float], None] = time.sleep,
    history_limit: int | None = None, batch_size: int = 500,
    max_messages_default: int = 20, min_delay_default: float = 300,
    max_delay_default: float = 600, provider_plan: str = "trial",
    daily_provider_cap: int = 50, start_time: day_time = day_time(9),
    end_time: day_time = day_time(19),
    allowed_weekdays: str = "MON,TUE,WED,THU,FRI",
    timezone_name: str = "America/Santiago",
    journal_path: str | Path | None = None,
) -> dict[str, Any]:
    """Guide an operator through read-only selection, individual approval and send."""
    events = _events(db, property_code, since=since, currency=currency,
                     operation=operation, limit=history_limit, batch_size=batch_size)
    if not events:
        output_fn("No hay rebajas vigentes para los filtros seleccionados.")
        return {"status": "NO_EVENTS", "prepared": 0, "attempts": 0}

    output_fn("Rebajas vigentes y consultas históricas verificadas (barrido en lotes):")
    for index, event in enumerate(events, 1):
        rows = find_candidates_for_event(db, event)
        eligible = sum(bool(row["eligible"]) for row in rows)
        output_fn(f"  {index}. {_display(event, eligible)} | elegibles={eligible} | excluidos={len(rows)-eligible}")
    chosen_events = [events[index - 1] for index in _menu_selection(
        input_fn("Elige propiedades/eventos por número (ej. 1,3 o all): "), len(events),
    )]

    selected_by_event: dict[str, tuple[PriceDropEvent, list[dict[str, Any]]]] = {}
    for event in chosen_events:
        rows = find_candidates_for_event(db, event)
        output_fn(f"\nPropiedad {event.property_code}: leads elegibles, excluidos y motivo")
        for index, row in enumerate(rows, 1):
            marker = "ELEGIBLE" if row["eligible"] else "EXCLUIDO"
            inquiry = row["inquiry_at"].astimezone(CHILE_TZ).isoformat() if row["inquiry_at"] else "N/A"
            output_fn(
                f"  {index}. {marker} | teléfono ***{row['phone_last4']} | "
                f"{row['temperature']} | consulta={inquiry} | "
                f"evidencia={row['inquiry_reference'] or 'N/A'} | motivo={row['reason']}"
            )
        eligible_indexes = [index for index, row in enumerate(rows, 1) if row["eligible"]]
        if not eligible_indexes:
            output_fn("  No hay destinatarios elegibles para esta rebaja.")
            continue
        allowed = set(eligible_indexes)
        raw = input_fn("Selecciona destinatarios elegibles por número (ej. 1,2): ")
        selected_indexes = _menu_selection(raw, len(rows))
        if any(index not in allowed for index in selected_indexes):
            raise CliError("selected_candidate_is_excluded_review_reason")
        selected = [rows[index - 1] for index in selected_indexes]
        selected_by_event[event.event_id] = (event, selected)
    if not selected_by_event:
        return {"status": "NO_SELECTED_RECIPIENTS", "prepared": 0, "attempts": 0}

    selected_count = sum(len(rows) for _, rows in selected_by_event.values())
    try:
        max_messages = int(input_fn(
            f"Máximo de mensajes [predeterminado {max_messages_default}]: "
        ).strip() or max_messages_default)
        min_delay = float(input_fn(
            f"Pausa mínima en segundos [predeterminado {min_delay_default:g}]: "
        ).strip() or min_delay_default)
        max_delay = float(input_fn(
            f"Pausa máxima en segundos [predeterminado {max_delay_default:g}]: "
        ).strip() or max_delay_default)
    except ValueError as exc:
        raise CliError("message_limit_and_delays_must_be_numbers") from exc
    validate_limits(max_messages, min_delay, max_delay,
                    provider_plan=provider_plan, daily_cap=daily_provider_cap)
    if start_time >= end_time:
        raise CliError("business_hours_start_must_precede_end")
    _weekdays(allowed_weekdays)

    previews = []
    for event, rows in selected_by_event.values():
        for candidate in rows:
            previews.append(_preview_message(db, event, candidate))
    output_fn(f"\nPrevisualización exacta de {len(previews)} mensajes seleccionados:")
    for index, preview in enumerate(previews, 1):
        candidate = preview["candidate"]
        output_fn(
            f"\n[{index}] propiedad {preview['event'].property_code} | "
            f"teléfono ***{candidate['phone_last4']} | {candidate['temperature']} | "
            f"consulta={candidate['inquiry_at']} | evidencia={candidate['inquiry_reference']}"
        )
        output_fn(preview["message"])

    publication_checks = {}
    for event, _ in selected_by_event.values():
        publication_checks[event.event_id] = _publication_result_dict(publication_verifier(
            db, property_code=event.property_code, currency=event.currency,
            expected_price=event.current_price, expected_url=event.listing_url,
            now=now_fn(),
        ))
    for event, _ in selected_by_event.values():
        check = publication_checks[event.event_id]
        output_fn(
            f"Verificación pública propiedad {event.property_code}: {check['status']}"
            + (f" ({check['reason']})" if check.get("reason") else "")
        )
    blocked = [row for row in publication_checks.values() if not row["sendable"]]
    if blocked:
        return {"status": "BLOCKED_PUBLICATION_REVIEW", "attempts": 0,
                "publication_checks": publication_checks}

    operator = input_fn("Operador responsable: ").strip()
    if not operator:
        raise CliError("operator_required")
    evidence_by_lead = {}
    for event, rows in selected_by_event.values():
        for candidate in rows:
            evidence_by_lead[candidate["lead_id"]] = input_fn(
                f"Referencia CRM del opt-in WhatsApp para propiedad {event.property_code}, "
                f"teléfono ***{candidate['phone_last4']}: "
            ).strip()
    if any(len(value) < 8 for value in evidence_by_lead.values()):
        raise CliError("whatsapp_contact_authorization_evidence_required_per_lead")
    if input_fn(
        f"Confirma revisión de consulta previa y consentimiento para {selected_count} contactos "
        f"(escribe CONFIRMO {selected_count}): "
    ).strip() != f"CONFIRMO {selected_count}":
        return {"status": "CANCELLED", "phase": "review", "prepared": 0, "attempts": 0}
    if input_fn(f"Escribe PREPARAR {selected_count} para crear campañas PREPARED: ").strip() \
            != f"PREPARAR {selected_count}":
        return {"status": "CANCELLED", "phase": "prepare", "prepared": 0, "attempts": 0}

    selected_lead_ids = [row["lead_id"] for _, rows in selected_by_event.values() for row in rows]
    prepare_backup = backup_fn(db, [_lead_id(value) for value in selected_lead_ids])
    lead_ids_by_event = {}
    for event_id, (_, rows) in selected_by_event.items():
        ids = [row["lead_id"] for row in rows]
        lead_ids_by_event[event_id] = ids
        event = next(event for event, _ in selected_by_event.values()
                     if event.event_id == event_id)
        prepare_selected(
            db, event=event, lead_ids=ids, operator=operator,
            contact_basis_reviewed=True, whatsapp_contact_reviewed=True,
            whatsapp_contact_evidence=evidence_by_lead,
        )
    output_fn("Campañas PREPARED. Mensajes exactos leídos desde los registros preparados:")
    approved_snapshots = {}
    for event_id, ids in lead_ids_by_event.items():
        rows = _entries(db, event_id, ("PREPARED",), set(ids))
        if len(rows) != len(ids):
            return {"status": "BLOCKED_PREPARE_READBACK_MISMATCH", "attempts": 0}
        approved_snapshots[event_id] = {
            str(row["lead"]["_id"]): row["campaign"]["outbound_message"] for row in rows
        }
        for row in rows:
            output_fn(f"\n[{row['campaign'].get('property_code')}] {row['campaign']['outbound_message']}")
    if input_fn(f"Escribe APROBAR {selected_count} para aprobar individualmente estas campañas: ").strip() \
            != f"APROBAR {selected_count}":
        return {"status": "PREPARED_PENDING_APPROVAL", "prepared": selected_count,
                "attempts": 0, "campaign_ids": list(lead_ids_by_event),
                "backup": str(prepare_backup)}

    approval_backup = backup_fn(db, [_lead_id(value) for value in selected_lead_ids])
    for event_id, ids in lead_ids_by_event.items():
        approve_selected(db, campaign_id=event_id, lead_ids=ids, operator=operator)
    if input_fn(f"Escribe ENVIAR {selected_count} para iniciar los intentos aprobados: ").strip() \
            != f"ENVIAR {selected_count}":
        return {"status": "APPROVED_PENDING_SEND", "prepared": selected_count,
                "attempts": 0, "campaign_ids": list(lead_ids_by_event),
                "backup": str(approval_backup)}

    campaign_items = list(lead_ids_by_event.items())
    total_attempts = 0
    results = []
    for campaign_index, (event_id, ids) in enumerate(campaign_items):
        remaining_budget = max_messages - total_attempts
        if remaining_budget <= 0:
            break
        event = next(event for event, _ in selected_by_event.values()
                     if event.event_id == event_id)
        env = {
            v2.SEND_FLAG: "true", v2.APPROVED_ID_FLAG: event_id,
            v2.CONTEXT_FLAG: "true", v2.REAL_CAMPAIGNS_ENABLED_FLAG: "true",
            v2.WHATSAPP_CONTACT_REVIEWED_FLAG: "true",
            "PRICE_DROP_WHATSAPP_CHANNEL_MODE": "wasenderapi",
        }
        with _temporary_environment(env):
            outcome = execute_campaign(
                db, campaign_id=event_id, max_messages=remaining_budget,
                min_delay_seconds=min_delay, max_delay_seconds=max_delay,
                confirm=lambda _prompt, cid=event_id: f"ENVIAR {cid}",
                start_time=start_time, end_time=end_time,
                allowed_weekdays=allowed_weekdays, timezone_name=timezone_name,
                provider_plan=provider_plan, daily_provider_cap=daily_provider_cap,
                provider_ready=provider_ready,
                live_links_verified=True, sender=sender, usage_reader=usage_reader,
                now_fn=now_fn,
                sleep_fn=sleep_fn,
                journal_path=journal_path,
                allowed_property_codes=None,
                link_verifier=lambda code, stamp, e=event: _publication_result_dict(publication_verifier(
                    db, property_code=code, currency=e.currency,
                    expected_price=e.current_price, expected_url=e.listing_url,
                    now=stamp,
                )),
                lead_allowlist=set(ids),
                pause_after_last=campaign_index < len(campaign_items) - 1,
                expected_messages=approved_snapshots[event_id],
            )
        results.append({"campaign_id": event_id, **outcome})
        total_attempts += int(outcome.get("attempts") or 0)
        if outcome.get("status") not in {"COMPLETED", "NO_APPROVED_RECIPIENTS"}:
            return {"status": outcome.get("status"), "prepared": selected_count,
                    "attempts": total_attempts, "pending": sum(
                        max(len(lead_ids_by_event[cid]) - sum(
                            1 for row in _entries(db, cid, ("SENT", "RESPONDED"), set(lead_ids_by_event[cid]))
                    ), 0) for cid in lead_ids_by_event
                    ), "campaign_results": results,
                    "backup": str(approval_backup)}
    total_pending = sum(len(_entries(db, campaign_id, ("APPROVED",), set(ids)))
                        for campaign_id, ids in lead_ids_by_event.items())
    return {"status": "LIMIT_REACHED" if total_pending else "COMPLETED",
            "prepared": selected_count, "attempts": total_attempts,
            "pending": total_pending, "campaign_results": results,
            "backup": str(approval_backup)}


def _report(db: Any, campaign_id: str | None = None) -> int:
    groups = _campaigns_by_status(db, (
        "PREPARED", "APPROVED", "SENDING", "SENT", "DELIVERY_UNKNOWN",
        "RESPONDED", "DECLINED", "OPTED_OUT", "BLOCKED",
    ))
    selected = ({campaign_id: groups.get(campaign_id, [])} if campaign_id else groups)
    for name in sorted(selected):
        summary = _status_report(selected[name])
        print(json.dumps(summary, ensure_ascii=False, default=str))
    if not selected:
        print("No hay campañas registradas.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Descubre rebajas Prop360 y opera campañas V2 aprobadas de forma manual."
    )
    actions = parser.add_mutually_exclusive_group()
    for flag in ("discover", "dry-run", "interactive", "execute", "resume", "prepare", "approve",
                 "review-whatsapp-contact", "report"):
        actions.add_argument("--" + flag, dest="action", action="store_const", const=flag)
    parser.add_argument("--status", action="store_true", help="consulta estado sin escritura")
    parser.add_argument("--pilot", action="store_true", help="restringe al piloto aprobado de tres propiedades")
    parser.add_argument("--campaign-id")
    parser.add_argument("--event-id")
    parser.add_argument("--lead-id", action="append", default=[])
    parser.add_argument("--property", dest="property_code")
    parser.add_argument("--since", help="fecha ISO 8601 con zona horaria")
    parser.add_argument("--currency", choices=("UF", "CLP"), help="filtra por moneda original")
    parser.add_argument("--operation", choices=("venta",),
                        help="filtro de operación; campañas de rebaja habilitadas para venta")
    parser.add_argument("--operator", default="")
    parser.add_argument("--contact-basis-reviewed", action="store_true")
    parser.add_argument("--whatsapp-contact-reviewed", action="store_true")
    parser.add_argument("--whatsapp-contact-evidence", default="")
    parser.add_argument("--confirm-action", default="")
    parser.add_argument("--max-messages", type=int,
                        default=_env_int("PRICE_DROP_MAX_MESSAGES_PER_RUN", 20))
    parser.add_argument("--min-delay-seconds", type=float,
                        default=float(os.getenv("PRICE_DROP_MIN_DELAY_SECONDS", "300")))
    parser.add_argument("--max-delay-seconds", type=float,
                        default=float(os.getenv("PRICE_DROP_MAX_DELAY_SECONDS", "600")))
    parser.add_argument("--timezone", default=os.getenv("PRICE_DROP_TIMEZONE", "America/Santiago"))
    parser.add_argument("--start-time", type=_parse_time,
                        default=_parse_time(os.getenv("PRICE_DROP_START_TIME", "09:00")))
    parser.add_argument("--end-time", type=_parse_time,
                        default=_parse_time(os.getenv("PRICE_DROP_END_TIME", "19:00")))
    parser.add_argument("--allowed-weekdays",
                        default=os.getenv("PRICE_DROP_ALLOWED_WEEKDAYS", "MON,TUE,WED,THU,FRI"))
    parser.add_argument("--provider-plan", choices=("trial", "paid"),
                        default=os.getenv("PRICE_DROP_WASENDER_PLAN", "trial").strip().lower())
    parser.add_argument("--daily-provider-cap", type=int,
                        default=_env_int("PRICE_DROP_DAILY_PROVIDER_CAP", 50))
    parser.add_argument("--limit", type=int, default=None,
                        help="límite técnico opcional; sin límite se procesa el historial completo")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="documentos procesados por lote durante el barrido completo")
    parser.add_argument("--full-scan", action="store_true",
                        help="lee todo el cursor histórico en lotes; prevalece sobre --limit")
    return parser


def _validate_cli(args: argparse.Namespace) -> datetime | None:
    if args.status and args.action:
        raise CliError("status_cannot_be_combined_with_another_action")
    if not args.status and not args.action:
        raise CliError("choose_an_action_or_status")
    if args.pilot and args.action not in {"execute", "resume", "review-whatsapp-contact", None} and not args.status:
        raise CliError("pilot_only_supports_status_execute_or_resume")
    since = parse_aware_datetime(args.since) if args.since else None
    if args.since and since is None:
        raise CliError("since_must_be_iso8601_with_timezone")
    if args.action in {"prepare", "approve", "review-whatsapp-contact"}:
        if not args.operator.strip() or not args.confirm_action.strip():
            raise CliError("operator_and_explicit_confirm_action_required")
    if args.action == "prepare" and not args.event_id:
        raise CliError("event_id_required_for_prepare")
    if args.action in {"approve", "review-whatsapp-contact"} and not args.campaign_id and not args.pilot:
        raise CliError("campaign_id_required")
    if args.action == "prepare" and (not args.contact_basis_reviewed
            or not args.whatsapp_contact_reviewed
            or len(args.whatsapp_contact_evidence.strip()) < 8):
        raise CliError("contact_basis_and_whatsapp_authorization_reviews_required")
    if (args.action in {"prepare", "approve", "review-whatsapp-contact"}
            and not args.lead_id and not (args.pilot and args.action == "review-whatsapp-contact")):
        raise CliError("lead_id_required")
    if args.action == "execute" and not args.pilot and not args.campaign_id:
        raise CliError("campaign_id_required_for_execution")
    return since


def _select_resume_campaign(db: Any) -> str:
    groups = _campaigns_by_status(db, ("APPROVED", "SENDING", "DELIVERY_UNKNOWN"))
    if len(groups) != 1:
        raise CliError("resume_requires_one_active_campaign_or_campaign_id")
    return next(iter(groups))


def _confirm_token(action: str, identity: str) -> str:
    return f"{action}_{identity}"


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if (not args.status and args.action is None
                and _truthy(os.getenv("PRICE_DROP_DRY_RUN_DEFAULT", "true"))):
            args.action = "dry-run"
        since = _validate_cli(args)
        if args.action in {"execute", "resume"}:
            validate_limits(args.max_messages, args.min_delay_seconds, args.max_delay_seconds,
                            provider_plan=args.provider_plan,
                            daily_cap=args.daily_provider_cap)
            if args.start_time >= args.end_time:
                raise CliError("business_hours_start_must_precede_end")
            _weekdays(args.allowed_weekdays)
        if args.full_scan:
            args.limit = None
        if args.limit is not None and args.limit < 1:
            raise CliError("limit_must_be_positive_or_unset")
        if not 1 <= args.batch_size <= 5000:
            raise CliError("batch_size_must_be_between_1_and_5000")
        if args.pilot:
            args.campaign_id = PILOT_CAMPAIGN_ID
        db = _get_db()
        if args.action == "resume" and not args.campaign_id:
            args.campaign_id = _select_resume_campaign(db)

        if args.status:
            if args.pilot:
                print(json.dumps(_pilot_status(db), ensure_ascii=False, default=str))
                return 0
            return _report(db, args.campaign_id)
        if args.action == "report":
            return _report(db, args.campaign_id)
        if args.action == "discover":
            return _print_events(db, detailed=False, since=since,
                                 property_code=args.property_code,
                                 currency=args.currency, operation=args.operation,
                                 limit=args.limit, batch_size=args.batch_size)
        if args.action == "dry-run":
            return _print_events(db, detailed=True, since=since,
                                 property_code=args.property_code,
                                 currency=args.currency, operation=args.operation,
                                 limit=args.limit, batch_size=args.batch_size)
        if args.action == "interactive":
            result = interactive_workflow(
                db, since=since, property_code=args.property_code,
                currency=args.currency, operation=args.operation,
                history_limit=args.limit, batch_size=args.batch_size,
                max_messages_default=args.max_messages,
                min_delay_default=args.min_delay_seconds,
                max_delay_default=args.max_delay_seconds,
                provider_plan=args.provider_plan,
                daily_provider_cap=args.daily_provider_cap,
                start_time=args.start_time, end_time=args.end_time,
                allowed_weekdays=args.allowed_weekdays,
                timezone_name=args.timezone,
            )
            print(json.dumps(result, ensure_ascii=False, default=str))
            return 0 if result.get("status") in {
                "COMPLETED", "LIMIT_REACHED", "CANCELLED", "NO_EVENTS",
                "NO_SELECTED_RECIPIENTS", "PREPARED_PENDING_APPROVAL",
                "APPROVED_PENDING_SEND",
            } else 1
        if args.action == "prepare":
            expected = _confirm_token("PREPARAR", args.event_id)
            if args.confirm_action != expected:
                raise CliError(f"type_exact_confirm_action:{expected}")
            event = _resolve_event(db, args.event_id, since=since,
                                   property_code=args.property_code)
            ids = [_lead_id(value) for value in dict.fromkeys(args.lead_id)]
            backup = _backup_leads(db, ids)
            result = prepare_selected(
                db, event=event, lead_ids=args.lead_id, operator=args.operator,
                contact_basis_reviewed=args.contact_basis_reviewed,
                whatsapp_contact_reviewed=args.whatsapp_contact_reviewed,
                whatsapp_contact_evidence=args.whatsapp_contact_evidence,
            )
            print(json.dumps({**result, "backup": str(backup)}, ensure_ascii=False, default=str))
            return 0
        if args.action == "approve":
            expected = _confirm_token("APROBAR", args.campaign_id)
            if args.confirm_action != expected:
                raise CliError(f"type_exact_confirm_action:{expected}")
            ids = [_lead_id(value) for value in dict.fromkeys(args.lead_id)]
            backup = _backup_leads(db, ids)
            result = approve_selected(db, campaign_id=args.campaign_id,
                                      lead_ids=args.lead_id, operator=args.operator)
            print(json.dumps({**result, "backup": str(backup)}, ensure_ascii=False, default=str))
            return 0
        if args.action == "review-whatsapp-contact":
            expected = _confirm_token("REVISAR_WHATSAPP", args.campaign_id)
            if args.confirm_action != expected:
                raise CliError(f"type_exact_confirm_action:{expected}")
            lead_ids = args.lead_id
            if args.pilot and not lead_ids:
                pilot_rows = _entries(db, PILOT_CAMPAIGN_ID, ("PREPARED", "APPROVED"))
                codes = {str(row["campaign"].get("property_code") or "") for row in pilot_rows}
                if len(pilot_rows) != 3 or codes != PILOT_PROPERTY_CODES:
                    raise CliError("pilot_whatsapp_review_requires_exact_three_unsent_rows")
                lead_ids = [str(row["lead"].get("_id")) for row in pilot_rows]
            ids = [_lead_id(value) for value in dict.fromkeys(lead_ids)]
            backup = _backup_leads(db, ids)
            result = review_selected_whatsapp_contact(
                db, campaign_id=args.campaign_id, lead_ids=lead_ids,
                operator=args.operator, evidence=args.whatsapp_contact_evidence,
            )
            print(json.dumps({**result, "backup": str(backup)}, ensure_ascii=False, default=str))
            return 0
        if args.action in {"execute", "resume"}:
            if args.pilot:
                pilot = _pilot_status(db)
                if (pilot["recipient_count"] != 3 or not pilot["pilot_property_set_complete"]):
                    raise CliError("pilot_must_contain_exactly_three_approved_property_rows")
                if args.action == "execute" and pilot["statuses"] != {"APPROVED": 3}:
                    raise CliError("pilot_execute_requires_three_approved_unsent_campaigns_use_resume")
                if args.action == "resume" and any(
                        status not in {"APPROVED", "SENT", "RESPONDED"}
                        for status in pilot["statuses"]):
                    raise CliError("pilot_has_uncertain_or_blocked_state_review_status_first")
            active = _entries(db, args.campaign_id, ("APPROVED",))
            if not active:
                print(json.dumps({"status": "NO_APPROVED_RECIPIENTS", "attempts": 0},
                                 ensure_ascii=False))
                return 0
            codes = {str(item["campaign"].get("property_code") or "") for item in active}
            if args.pilot and not codes <= PILOT_PROPERTY_CODES:
                raise CliError("pilot_campaign_contains_unapproved_property")
            now = datetime.now(timezone.utc)
            expected_by_code = {
                str(item["campaign"].get("property_code") or ""):
                _expected_publication_from_campaign(item["campaign"])
                for item in active
            }
            publication_checks = _verify_current_links(db, expected_by_code, now)
            print(json.dumps({"publication_checks": publication_checks},
                             ensure_ascii=False, default=str))
            blocked_check = next((check for check in publication_checks.values()
                                  if not check.get("sendable")), None)
            if blocked_check:
                raise CliError("public_listing_verification_failed:" +
                               str(blocked_check.get("status")))
            result = execute_campaign(
                db, campaign_id=args.campaign_id,
                max_messages=args.max_messages,
                min_delay_seconds=args.min_delay_seconds,
                max_delay_seconds=args.max_delay_seconds,
                start_time=args.start_time, end_time=args.end_time,
                allowed_weekdays=args.allowed_weekdays,
                timezone_name=args.timezone, provider_plan=args.provider_plan,
                daily_provider_cap=args.daily_provider_cap,
                live_links_verified=True,
                allowed_property_codes=PILOT_PROPERTY_CODES if args.pilot else None,
                link_verifier=lambda code, stamp: _verify_current_links(
                    db, {code: expected_by_code[code]}, stamp,
                )[code],
                lead_allowlist=set(args.lead_id) if args.lead_id else None,
            )
            print(json.dumps(result, ensure_ascii=False, default=str))
            return 0 if result.get("status") in {
                "COMPLETED", "LIMIT_REACHED", "CANCELLED", "NO_APPROVED_RECIPIENTS"
            } else 1
        raise CliError("unsupported_action")
    except (CliError, v2.PilotBlocked, ValueError) as exc:
        safe = str(exc).replace("\n", " ")[:240]
        print(json.dumps({"status": "BLOCKED", "reason": safe}, ensure_ascii=False))
        return 2
    except Exception as exc:
        # Database/provider exceptions can contain credentials or endpoint URIs.
        print(json.dumps({"status": "ERROR", "reason": type(exc).__name__},
                         ensure_ascii=False))
        return 1


def _get_db():
    from config import Config
    if not Config.MONGO_URI:
        raise CliError("MONGO_URI_not_configured_use_PRICE_DROP_ENV_FILE")
    from chatbot.storage import get_db
    return get_db()


if __name__ == "__main__":
    raise SystemExit(main())
