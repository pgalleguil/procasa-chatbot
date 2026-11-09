"""Manual CLI for PROCASA price-drop discovery and approved V2 campaigns."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
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


def _day_start_utc(now: datetime, tz: ZoneInfo) -> datetime:
    return _local_now(now, tz).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).astimezone(timezone.utc)


def _provider_attempts_today(db: Any, *, now: datetime, tz: ZoneInfo) -> int:
    start = _day_start_utc(now, tz)
    try:
        rows = list(db[LEADS_COLLECTION].aggregate([
            {"$unwind": "$price_drop_v2.campaigns"},
            {"$match": {"price_drop_v2.campaigns.attempted_at": {"$gte": start}}},
            {"$count": "attempts"},
        ]))
        return int(rows[0]["attempts"]) if rows else 0
    except Exception as exc:
        raise CliError("provider_attempt_count_unavailable_fail_closed") from exc


def _entries(db: Any, campaign_id: str,
             statuses: tuple[str, ...] = ("APPROVED",)) -> list[dict[str, Any]]:
    docs = db[LEADS_COLLECTION].find(
        {"price_drop_v2.campaigns": {"$elemMatch": {
            "campaign_id": campaign_id, "status": {"$in": list(statuses)},
        }}},
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


def _events(db: Any, property_code: str | None = None) -> list[PriceDropEvent]:
    rows = discover_price_drops(db)
    return [row for row in rows if not property_code or row.property_code == property_code]


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


def _verify_current_links(db: Any, codes: set[str], now: datetime) -> bool:
    from chatbot.price_drop_campaign_discovery import _published
    for code in codes:
        prop = db["universo_cartera_prop360"].find_one(
            {"codigo": code}, {"codigo": 1, "disponible_prop360": 1,
             "estado.oficina": 1, "tipo_operacion": 1, "publicaciones": 1},
        ) or {}
        if (prop.get("disponible_prop360") is not True
                or (prop.get("estado") or {}).get("oficina") != "PROCASA SUCRE"
                or not _published(prop, now)[0]):
            return False
    return True
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
    if mode not in {"wasenderapi", "official"}:
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
    journal_path: str | Path | None = None,
    rng: Callable[[float, float], float] = random.uniform,
    allowed_property_codes: set[str] | None = None,
    link_verifier: Callable[[str, datetime], bool] | None = None,
) -> dict[str, Any]:
    validate_limits(max_messages, min_delay_seconds, max_delay_seconds,
                    provider_plan=provider_plan, daily_cap=daily_provider_cap)
    if start_time >= end_time:
        raise CliError("business_hours_start_must_precede_end")
    tz = ZoneInfo(timezone_name)
    config = {"timezone": tz, "weekdays": _weekdays(allowed_weekdays),
              "start": start_time, "end": end_time}
    if not live_links_verified:
        return {"status": "BLOCKED", "reason": "current_published_link_not_verified", "attempts": 0}
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
    uncertain = _entries(db, campaign_id, ("SENDING", "DELIVERY_UNKNOWN"))
    if uncertain:
        return {"status": "BLOCKED", "reason": "uncertain_delivery_requires_reconciliation",
                "uncertain_count": len(uncertain), "attempts": 0}

    entries = _entries(db, campaign_id, ("APPROVED",))
    if not entries:
        return {"status": "NO_APPROVED_RECIPIENTS", "attempts": 0, "pending": 0}
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
    with _execution_lock(journal_lock):
        # Re-read after the operator confirms and while holding the local lock.
        current_entries = _entries(db, campaign_id, ("APPROVED",))
        initial = {str(row["lead"]["_id"]): row["campaign"].get("outbound_message")
                   for row in entries}
        current = {str(row["lead"]["_id"]): row["campaign"].get("outbound_message")
                   for row in current_entries}
        if current != initial:
            return {"status": "BLOCKED", "reason": "campaign_changed_after_preview", "attempts": 0}
        entries = current_entries
        daily_before = _provider_attempts_today(db, now=now_fn(), tz=tz)
        run_limit = min(max_messages, max(daily_provider_cap - daily_before, 0))
        if run_limit == 0:
            return {"status": "BLOCKED", "reason": "daily_provider_limit_reached",
                    "attempts": 0, "pending": len(entries)}
        for index, entry in enumerate(entries):
            if attempts >= max_messages:
                break
            now = now_fn()
            daily_current = _provider_attempts_today(db, now=now, tz=tz)
            if daily_current >= daily_provider_cap:
                break
            if link_verifier is not None and not link_verifier(
                    str(entry["campaign"].get("property_code") or ""), now):
                results.append({"status": "BLOCKED", "reason": "current_published_link_not_verified",
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
        pending = len(_entries(db, campaign_id, ("APPROVED",)))
        daily_after = _provider_attempts_today(db, now=now_fn(), tz=tz)
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
                     whatsapp_contact_evidence: str) -> dict[str, Any]:
    if not operator.strip():
        raise CliError("operator_required")
    if not contact_basis_reviewed:
        raise CliError("contact_basis_must_be_reviewed")
    if not whatsapp_contact_reviewed or len(whatsapp_contact_evidence.strip()) < 8:
        raise CliError("whatsapp_contact_authorization_review_required")
    if not lead_ids:
        raise CliError("at_least_one_lead_id_required")
    candidates = {row["lead_id"]: row for row in find_candidates_for_event(db, event)}
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
            operator=operator, evidence=whatsapp_contact_evidence,
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
    matches = [event for event in _events(db, property_code)
               if event.event_id == event_id and (since is None or event.dropped_at >= since)]
    if len(matches) != 1:
        raise CliError("current_price_drop_event_not_found_or_ambiguous")
    return matches[0]


def _print_events(db: Any, *, detailed: bool, since=None,
                  property_code: str | None = None) -> int:
    events = _events(db, property_code)
    if since:
        events = [event for event in events if event.dropped_at >= since]
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
    for flag in ("discover", "dry-run", "execute", "resume", "prepare", "approve",
                 "review-whatsapp-contact", "report"):
        actions.add_argument("--" + flag, dest="action", action="store_const", const=flag)
    parser.add_argument("--status", action="store_true", help="consulta estado sin escritura")
    parser.add_argument("--pilot", action="store_true", help="restringe al piloto aprobado de tres propiedades")
    parser.add_argument("--campaign-id")
    parser.add_argument("--event-id")
    parser.add_argument("--lead-id", action="append", default=[])
    parser.add_argument("--property", dest="property_code")
    parser.add_argument("--since", help="fecha ISO 8601 con zona horaria")
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
    parser.add_argument("--limit", type=int, default=500,
                        help="máximo de eventos históricos leídos (1 a 5000)")
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
        if args.limit < 1 or args.limit > 5000:
            raise CliError("limit_must_be_between_1_and_5000")
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
                                 property_code=args.property_code)
        if args.action == "dry-run":
            return _print_events(db, detailed=True, since=since,
                                 property_code=args.property_code)
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
            if not _verify_current_links(db, codes, now):
                raise CliError("current_published_link_not_verified_for_every_recipient")
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
                link_verifier=lambda code, stamp: _verify_current_links(db, {code}, stamp),
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
