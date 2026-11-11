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
    format_price, message_variant_id, parse_aware_datetime, stable_event_id,
)
from chatbot.price_drop_campaign_filters import (
    CampaignFilterError, effective_temperature, lead_age_details,
    normalize_lead_max_age_days, normalize_temperature_filter,
    temperature_filter_matches, temperature_review_is_required,
)
from chatbot.price_drop_reconciliation import (
    default_journal_path, pending_receipts, reconcile_pending_receipts,
)
from chatbot.price_drop_publication_check import verify_current_listing
from chatbot.wasender_usage import account_attempts_last_24h, WaSenderUsageUnavailable
from chatbot.price_drop_campaign_real import _lead_id

PILOT_CAMPAIGN_ID = "pilot_real_rebajas_20261109_v2"
PILOT_PROPERTY_CODES = {"6436", "6377", "16479"}
PILOT_PRICE_WINDOWS = {
    "6436": ("UF", 2790, 2690),
    "6377": ("UF", 5123, 5074),
    "16479": ("CLP", 160000000, 152000000),
}
_CAMPAIGN_REPORT_STATES = (
    "PREPARED", "APPROVED", "SENDING", "SENT", "FAILED", "DELIVERY_UNKNOWN",
    "RESPONDED", "DECLINED", "OPTED_OUT", "BLOCKED", "SUPERSEDED",
)
DEFAULT_LEAD_MAX_AGE_DAYS = 120
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


def _parse_temperature_arg(value: str) -> str:
    try:
        return normalize_temperature_filter(value)
    except CampaignFilterError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_lead_age_arg(value: str) -> int:
    try:
        days = int(value)
        normalize_lead_max_age_days(days)
        return days
    except (ValueError, CampaignFilterError) as exc:
        raise argparse.ArgumentTypeError(
            "use 0 for explicitly unlimited or a whole number from 1 to 3650"
        ) from exc


def _temperature_menu_value(value: str) -> str:
    choices = {
        "1": "HOT", "2": "NORMAL", "3": "COLD",
        "4": "HOT,NORMAL", "5": "ALL",
    }
    raw = str(value or "").strip()
    return choices[raw] if raw in choices else normalize_temperature_filter(raw or "ALL")


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
    personal_access_token = str(Config.WASENDER_PERSONAL_ACCESS_TOKEN or "").strip()
    if not personal_access_token:
        raise CliError("wasender_personal_access_token_required_for_account_usage")
    try:
        return account_attempts_last_24h(
            now=now, base_url=str(Config.WASENDER_BASE_URL or ""),
            personal_access_token=personal_access_token,
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
         "created_at": 1, "lead_temperature_effective": 1, "lead_temperature": 1,
         "price_drop_v2.campaigns": 1, "price_drop_v2.opted_out": 1,
         "price_drop_v2.whatsapp_contact_authorization": 1,
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


def _has_whatsapp_contact_authorization(entry: dict[str, Any], *, now=None) -> bool:
    """Accept campaign proof or a valid reusable lead authorization.

    If a lead-level record exists but is expired, mismatched, or revoked, do
    not let a stale campaign snapshot override that invalidation.
    """
    lead = entry.get("lead") or {}
    campaign = entry.get("campaign") or {}
    price_drop = lead.get("price_drop_v2") or {}
    auth_record = price_drop.get("whatsapp_contact_authorization") if isinstance(
        price_drop, dict
    ) else None
    reusable = v2.reusable_whatsapp_contact_authorization(
        lead, phone=str(campaign.get("phone_normalized") or ""), now=now,
    )
    if auth_record is not None and reusable is None:
        return False
    campaign_proof = (
        campaign.get("whatsapp_contact_reviewed") is True
        and bool(str(campaign.get("whatsapp_contact_evidence") or "").strip())
    )
    return campaign_proof or reusable is not None


def _campaigns_by_status(db: Any, statuses: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    docs = db[LEADS_COLLECTION].find(
        {"price_drop_v2.campaigns.status": {"$in": list(statuses)}},
        {"_id": 1, "phone": 1, "contact_phone_normalized": 1,
         "lead_temperature_effective": 1, "lead_temperature": 1,
         "created_at": 1, "conversation_status": 1,
         "messages": 1, "price_drop_v2": 1},
    )
    if hasattr(docs, "batch_size"):
        docs = docs.batch_size(500)
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
    tail = "" if interested is None else f" | leads elegibles: {interested}"
    local = event.dropped_at.astimezone(CHILE_TZ)
    return (f"{event.property_code} | {event.property_type} | {event.comuna} | "
            f"{format_price(event.old_price,event.currency)} → "
            f"{format_price(event.new_price,event.currency)} (-{event.percent_reduction:.2f}%) | "
            f"{local:%d/%m/%Y %H:%M %Z}{tail}")



def audit_full_portfolio(
    db: Any, *, since: datetime | None = None,
    property_code: str | None = None,
    currency: str | None = None, operation: str | None = None,
    temperature_filter: str = "ALL", lead_max_age_days: int | None = 120,
    limit: int | None = None, batch_size: int = 500,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only inventory of ALL price drops and prior-property inquiries.

    This is an audience discovery report, NOT send approval. It never
    validates or grants marketing permission, never invokes WaSender,
    never creates campaigns, and never writes MongoDB.
    """
    stamp = now or datetime.now(timezone.utc)
    normalized_temperature = normalize_temperature_filter(temperature_filter)
    max_age = normalize_lead_max_age_days(lead_max_age_days)
    events = _events(db, property_code, since=since, currency=currency,
                     operation=operation, limit=limit, batch_size=batch_size)
    unique_eligible_leads: set[str] = set()
    previously_counted: set[str] = set()
    reasons: Counter[str] = Counter()
    properties: set[str] = set()
    rows_by_event: list[dict[str, Any]] = []
    totals = {
        "events_found": len(events),
        "property_codes": 0,
        "candidate_event_matches": 0,
        "verified_inquiries_before_drop": 0,
        "eligible_event_matches": 0,
        "unique_eligible_leads": 0,
        "cross_event_duplicate_matches": 0,
        "excluded_or_review_needed": 0,
        "requires_temperature_review": 0,
        "requires_age_review": 0,
    }
    for event in events:
        properties.add(event.property_code)
        candidates = find_candidates_for_event(
            db, event, temperature_filter=normalized_temperature,
            lead_max_age_days=max_age, now=stamp,
        )
        count = 0
        for row in candidates:
            totals["candidate_event_matches"] += 1
            reasons[str(row.get("reason") or "unknown")] += 1
            if row.get("inquiry_at") is not None:
                totals["verified_inquiries_before_drop"] += 1
            if row.get("temperature_review_required"):
                totals["requires_temperature_review"] += 1
            if row.get("age_review_required"):
                totals["requires_age_review"] += 1
            if row.get("eligible"):
                count += 1
                totals["eligible_event_matches"] += 1
                lead_id = str(row["lead_id"])
                if lead_id in previously_counted:
                    totals["cross_event_duplicate_matches"] += 1
                previously_counted.add(lead_id)
                unique_eligible_leads.add(lead_id)
            else:
                totals["excluded_or_review_needed"] += 1
        rows_by_event.append({
            "event_id": event.event_id,
            "property_code": event.property_code,
            "comuna": event.comuna,
            "currency": event.currency,
            "old_price": event.old_price,
            "new_price": event.new_price,
            "dropped_at": event.dropped_at.isoformat(),
            "candidates": len(candidates),
            "eligible": count,
            "excluded_or_review_needed": len(candidates) - count,
        })
    totals["property_codes"] = len(properties)
    totals["unique_eligible_leads"] = len(unique_eligible_leads)
    return {
        "scope": "entire_property_price_drop_history" if limit is None else "diagnostic_limited_scan",
        "database_mode": "READ_ONLY",
        "send_approved": False,
        "sending_attempts": 0,
        "reviewed_whatsapp_authorizations": "NOT_VERIFIED_BY_THIS_AUDIT",
        "filters": {
            "since": since.isoformat() if isinstance(since, datetime) else None,
            "property_code": property_code, "currency": currency,
            "operation": operation, "temperature": normalized_temperature,
            "lead_max_age_days": max_age,
        },
        "totals": totals,
        "exclusion_reasons": dict(reasons.most_common()),
        "events": sorted(rows_by_event, key=lambda x: (
            -x["eligible"], x["property_code"], x["event_id"]
        )),
        "next_step": "Review permission evidence per lead; no recipient is approved by this audit",
    }



def _pilot_status(db: Any) -> dict[str, Any]:
    rows = _entries(db, PILOT_CAMPAIGN_ID, _CAMPAIGN_REPORT_STATES)
    codes = {str(row["campaign"].get("property_code") or "") for row in rows}
    history_count = sum(
        1 for row in rows for message in row["lead"].get("messages") or []
        if isinstance(message, dict)
        and (message.get("metadata") or {}).get("campaign_id") == PILOT_CAMPAIGN_ID
    )
    return {**_status_report(rows), "pilot_property_set_complete": codes == PILOT_PROPERTY_CODES,
            "messages_in_history": history_count}


def _validated_pilot_rows(db: Any) -> list[dict[str, Any]]:
    """Return only the three persisted, property-and-price-pinned pilot rows."""
    rows = _entries(db, PILOT_CAMPAIGN_ID, _CAMPAIGN_REPORT_STATES)
    codes = [str(row["campaign"].get("property_code") or "") for row in rows]
    if len(rows) != 3 or set(codes) != PILOT_PROPERTY_CODES or len(set(codes)) != 3:
        raise CliError("pilot_must_contain_exactly_three_persisted_property_rows")
    lead_ids = [str(row["lead"].get("_id")) for row in rows]
    if len(set(lead_ids)) != 3:
        raise CliError("pilot_recipient_identity_must_be_unique")
    for row in rows:
        campaign = row["campaign"]
        code = str(campaign.get("property_code") or "")
        expected_currency, expected_old, expected_new = PILOT_PRICE_WINDOWS[code]
        currency = str(campaign.get("currency") or "UF").upper()
        old_price = (campaign.get("old_price_clp") if currency == "CLP"
                     else campaign.get("old_price_uf"))
        new_price = (campaign.get("new_price_clp") if currency == "CLP"
                     else campaign.get("new_price_uf"))
        if (currency, old_price, new_price) != (
                expected_currency, expected_old, expected_new):
            raise CliError("pilot_property_price_window_mismatch")
    return rows


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
    urls = re.findall(r"https://[^\s<>]+", str(campaign.get("outbound_message") or ""), re.I)
    url = urls[0].rstrip(".,);]") if urls else ""
    return {"currency": currency, "price": price, "url": url}


def _publication_result_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "as_dict"):
        return result.as_dict()
    if isinstance(result, dict):
        return result
    raise CliError("publication_verifier_result_invalid")


def _default_progress_path() -> Path:
    journal = default_journal_path()
    return journal.with_name("price_drop_execution_progress.jsonl")


def _append_progress(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _progress_line(*, attempts: int, max_messages: int, accepted: int,
                   failed: int, uncertain: int, delivered: int, read: int,
                   responses: int, pending_batch: int, pending_total: int,
                   property_code: str, status: str, next_attempt: datetime | None,
                   pause_seconds: float | None, timezone_name: str) -> str:
    denominator = max(max_messages, 1)
    completed = min(attempts, denominator)
    width = 20
    filled = int(width * completed / denominator)
    bar = "#" * filled + "-" * (width - filled)
    local_next = (next_attempt.astimezone(ZoneInfo(timezone_name)).strftime("%H:%M")
                  if next_attempt else "N/A")
    pause = f"{pause_seconds:.0f} segundos" if pause_seconds is not None else "N/A"
    return (
        f"[{bar}] {completed}/{max_messages} ({100 * completed / denominator:.0f}%) | "
        f"Intentos: {attempts} | Aceptados WaSender: {accepted} | Fallidos confirmados: {failed} | "
        f"Entregados: {delivered} | Leídos: {read} | Respuestas: {responses} | "
        f"Inciertos: {uncertain} | Pendientes en lote: {pending_batch} | "
        f"Pendientes totales: {pending_total} | Última propiedad: {property_code or 'N/D'} | "
        f"Último estado: {status} | Próximo envío: {local_next} | Pausa seleccionada: {pause}"
    )


def _temperature_set(value: str) -> set[str]:
    normalized = normalize_temperature_filter(value)
    return {"HOT", "NORMAL", "COLD"} if normalized == "ALL" else set(normalized.split(","))


def _latest_event_by_code(db: Any, property_codes: set[str]) -> dict[str, PriceDropEvent]:
    result = {}
    for code in property_codes:
        events = _events(db, property_code=code, limit=None, batch_size=500)
        if events:
            result[code] = max(events, key=lambda event: (event.dropped_at, event.event_id))
    return result


def _supersede_stale_pending_campaigns(
    db: Any, property_codes: set[str], *, now: datetime,
) -> dict[str, Any]:
    """Mark only unsent PREPARED/APPROVED rows whose property has a newer drop."""
    if not property_codes:
        return {"count": 0, "campaign_ids": []}
    latest = _latest_event_by_code(db, property_codes)
    pending_states = ("PREPARED", "APPROVED")
    query = {"price_drop_v2.campaigns": {"$elemMatch": {
        "property_code": {"$in": list(property_codes)},
        "status": {"$in": list(pending_states)},
    }}}
    docs = db[LEADS_COLLECTION].find(
        query, {"_id": 1, "price_drop_v2.campaigns": 1},
    )
    if hasattr(docs, "batch_size"):
        docs = docs.batch_size(500)
    superseded = []
    for lead in docs:
        for index, campaign in enumerate((lead.get("price_drop_v2") or {}).get("campaigns") or []):
            code = str(campaign.get("property_code") or "")
            current = latest.get(code)
            old_event_id = str(campaign.get("event_id") or campaign.get("campaign_id") or "")
            if (not current or campaign.get("status") not in pending_states
                    or old_event_id == current.event_id):
                continue
            raw_old_stamp = campaign.get("price_drop_at")
            if isinstance(raw_old_stamp, datetime):
                old_stamp = (raw_old_stamp.replace(tzinfo=timezone.utc)
                             if raw_old_stamp.tzinfo is None or raw_old_stamp.utcoffset() is None
                             else raw_old_stamp.astimezone(timezone.utc))
            else:
                old_stamp = parse_aware_datetime(raw_old_stamp)
            if old_stamp is not None and current.dropped_at <= old_stamp:
                continue
            campaign_id = str(campaign.get("campaign_id") or "")
            update = db[LEADS_COLLECTION].update_one({
                "_id": lead["_id"],
                "price_drop_v2.campaigns": {"$elemMatch": {
                    "campaign_id": campaign_id,
                    "status": {"$in": list(pending_states)},
                }},
            }, {"$set": {
                f"price_drop_v2.campaigns.{index}.status": "SUPERSEDED",
                f"price_drop_v2.campaigns.{index}.superseded_at": now,
                f"price_drop_v2.campaigns.{index}.superseded_by_event_id": current.event_id,
                f"price_drop_v2.campaigns.{index}.superseded_reason": "newer_property_price_drop",
            }})
            if update.modified_count == 1:
                superseded.append(campaign_id)
    return {"count": len(superseded), "campaign_ids": superseded,
            "new_event_ids": sorted({event.event_id for event in latest.values()})}


def _campaign_filter_blocker(entry: dict[str, Any], *, temperature_filter: str | None,
                             lead_max_age_days: int | None, now: datetime,
                             enforce_filters: bool) -> str | None:
    campaign = entry["campaign"]
    if campaign.get("test_mode") or not (
        enforce_filters or campaign.get("campaign_filters_enabled") is True
    ):
        return None
    review = campaign.get("temperature_review") or {}
    stored_filter = normalize_temperature_filter(campaign.get("temperature_filter", "ALL"))
    selected_filter = normalize_temperature_filter(
        temperature_filter if temperature_filter is not None else stored_filter
    )
    if campaign.get("campaign_filters_enabled") is True and not (
        _temperature_set(selected_filter) <= _temperature_set(stored_filter)
    ):
        return "temperature_filter_would_widen_approved_campaign"
    lead = entry["lead"]
    effective = effective_temperature(lead, review)
    if effective == "REVISIÓN_PENDIENTE" or temperature_review_is_required(lead, review):
        return "temperature_review_pending"
    current = str(lead.get("lead_temperature_effective") or lead.get("lead_temperature") or "").strip().upper()
    if review and current in {"HOT", "NORMAL", "COLD"}:
        return "temperature_changed_after_manual_review"
    if not temperature_filter_matches(effective, selected_filter):
        return "temperature_filter_mismatch"
    use_override = enforce_filters
    selected_age_raw = (
        lead_max_age_days if use_override and lead_max_age_days is not None
        else campaign.get("lead_max_age_days", DEFAULT_LEAD_MAX_AGE_DAYS)
    )
    selected_age = normalize_lead_max_age_days(selected_age_raw)
    stored_age = normalize_lead_max_age_days(campaign.get("lead_max_age_days"))
    if (campaign.get("campaign_filters_enabled") is True and use_override
            and stored_age is not None
            and (selected_age is None or selected_age > stored_age)):
        return "lead_age_filter_would_widen_approved_campaign"
    age = lead_age_details(lead, now=now, max_age_days=selected_age)
    if age["lead_age_status"] != "valid":
        return str(age["lead_age_status"])
    if age["excluded_by_age"]:
        return "lead_age_exceeded"
    return None


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
    temperature_filter: str | None = None,
    lead_max_age_days: int | None = None,
    enforce_campaign_filters: bool = False,
    progress_path: str | Path | None = None,
    progress_fn: Callable[[str], None] | None = None,
    progress_offset: int = 0,
    progress_total_limit: int | None = None,
    display_max_messages: int | None = None,
    pending_campaigns: dict[str, set[str]] | None = None,
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
    if os.getenv(v2.SEND_FLAG, "").strip().lower() not in {"1", "true", "yes"}:
        return {"status": "BLOCKED", "reason": "v2_campaign_runtime_gate_disabled", "attempts": 0}
    if os.getenv(v2.CONTEXT_FLAG, "").strip().lower() not in {"1", "true", "yes"}:
        return {"status": "BLOCKED", "reason": "campaign_context_disabled", "attempts": 0}
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

    stale_result = {"count": 0, "campaign_ids": []}
    property_codes = {
        str(row["campaign"].get("property_code") or "")
        for row in _entries(db, campaign_id, ("PREPARED", "APPROVED"), lead_allowlist)
        if row["campaign"].get("property_code")
    }
    stale_result = _supersede_stale_pending_campaigns(db, property_codes, now=now)
    entries = _entries(db, campaign_id, ("APPROVED",), lead_allowlist)
    if not entries:
        if stale_result["count"]:
            return {"status": "STOPPED_STALE_CAMPAIGN", "attempts": 0,
                    "superseded_count": stale_result["count"],
                    "superseded_campaign_ids": stale_result["campaign_ids"]}
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
    if any(not _has_whatsapp_contact_authorization(row, now=now) for row in entries):
        return {"status": "BLOCKED", "reason": "whatsapp_contact_authorization_unverified",
                "attempts": 0}
    for entry in entries:
        reason = _campaign_filter_blocker(
            entry, temperature_filter=temperature_filter,
            lead_max_age_days=lead_max_age_days, now=now,
            enforce_filters=enforce_campaign_filters,
        )
        if reason:
            return {"status": "BLOCKED", "reason": reason, "attempts": 0}
    for entry in entries:
        reason = _message_window_blocker(entry, now)
        if reason:
            return {"status": "BLOCKED", "reason": reason, "attempts": 0}

    display_campaign = entries[0]["campaign"]
    selected_filter = normalize_temperature_filter(
        temperature_filter if temperature_filter is not None
        else display_campaign.get("temperature_filter", "ALL")
    )
    selected_age = normalize_lead_max_age_days(
        lead_max_age_days if lead_max_age_days is not None
        else display_campaign.get("lead_max_age_days", DEFAULT_LEAD_MAX_AGE_DAYS)
    )
    age_label = selected_age if selected_age is not None else "sin límite"
    print("PROCASA — CAMPAÑA DE REBAJAS")
    print(f"Temperatura seleccionada: {selected_filter}")
    print(f"Antigüedad máxima: {age_label} días")
    display_limit = display_max_messages or progress_total_limit or max_messages
    print(f"Aprobados pendientes: {len(entries)} | Máximo por ejecución: {display_limit} | "
          f"Pendientes estimados después del lote: {max(len(entries)-display_limit, 0)}")
    if stale_result["count"]:
        print(f"Campañas antiguas sustituidas: {stale_result['count']}")
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
    results, attempts, accepted_count, failed_count, uncertain_count = [], 0, 0, 0, 0
    delivered_count, read_count, response_count = 0, 0, 0
    daily_before, run_limit = 0, 0
    run_started = now_fn()
    progress_file = Path(progress_path) if progress_path else _default_progress_path()
    run_id = f"{campaign_id}:{run_started.astimezone(timezone.utc).isoformat()}"
    emit_progress = progress_fn or (lambda line: print("\r" + line, end="", flush=True))

    def pending_campaign_count() -> int:
        if not pending_campaigns:
            return len(_entries(db, campaign_id, ("APPROVED",), lead_allowlist))
        return sum(len(_entries(db, cid, ("APPROVED",), ids))
                   for cid, ids in pending_campaigns.items())

    def campaign_state_rows() -> list[dict[str, Any]]:
        states = ("PREPARED", "APPROVED", "SENDING", "SENT", "FAILED",
                  "DELIVERY_UNKNOWN", "RESPONDED", "DECLINED", "OPTED_OUT")
        if not pending_campaigns:
            return _entries(db, campaign_id, states, lead_allowlist)
        return [row for cid, ids in pending_campaigns.items()
                for row in _entries(db, cid, states, ids)]
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
        stale_result = _supersede_stale_pending_campaigns(
            db, {str(row["campaign"].get("property_code") or "")
                 for row in entries if row["campaign"].get("property_code")},
            now=now_fn(),
        )
        entries = _entries(db, campaign_id, ("APPROVED",), lead_allowlist)
        if not entries:
            return {"status": "STOPPED_STALE_CAMPAIGN", "attempts": 0,
                    "superseded_count": stale_result["count"],
                    "superseded_campaign_ids": stale_result["campaign_ids"]}
        for entry in entries:
            reason = _campaign_filter_blocker(
                entry, temperature_filter=temperature_filter,
                lead_max_age_days=lead_max_age_days, now=now_fn(),
                enforce_filters=enforce_campaign_filters,
            )
            if reason:
                return {"status": "BLOCKED", "reason": reason, "attempts": 0}
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
        batch_pending_initial = pending_campaign_count()
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
            last_stale_check = _supersede_stale_pending_campaigns(
                db, {str(entry["campaign"].get("property_code") or "")}, now=now,
            )
            if last_stale_check["count"]:
                return {"status": "STOPPED_STALE_CAMPAIGN", "attempts": attempts,
                        "pending": len(_entries(db, campaign_id, ("APPROVED",), lead_allowlist)),
                        "superseded_count": last_stale_check["count"],
                        "results": results}
            reason = _campaign_filter_blocker(
                entry, temperature_filter=temperature_filter,
                lead_max_age_days=lead_max_age_days, now=now,
                enforce_filters=enforce_campaign_filters,
            )
            if reason:
                results.append({"status": "BLOCKED", "reason": reason,
                                "lead_last6": str(entry["lead"].get("_id"))[-6:]})
                continue
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
                temperature_filter_override=temperature_filter,
                lead_max_age_days_override=lead_max_age_days,
                enforce_campaign_filters=enforce_campaign_filters,
            )
            status = str(outcome.get("status") or "UNKNOWN")
            provider_status = str(outcome.get("provider_status") or "unknown").lower()
            results.append({
                "status": status, "lead_last6": str(entry["lead"].get("_id"))[-6:],
                "provider_id_present": bool(outcome.get("provider_message_id")),
            })
            if (outcome.get("provider_attempted") is True or status in {
                    "SENT", "FAILED", "DELIVERY_UNKNOWN", "HISTORY_RECONCILIATION_REQUIRED"}):
                attempts += 1
            if status == "SENT":
                accepted_count += 1
            if status == "FAILED" or provider_status == "failed":
                failed_count += 1
            if status in {"DELIVERY_UNKNOWN", "HISTORY_RECONCILIATION_REQUIRED"}:
                uncertain_count += 1
            if provider_status in {"delivered", "read", "seen", "played"}:
                delivered_count += 1
            if provider_status in {"read", "seen", "played"}:
                read_count += 1
            campaign_rows = campaign_state_rows()
            accepted_count = max(accepted_count, sum(
                1 for row in campaign_rows
                if row["campaign"].get("status") in {
                    "SENT", "RESPONDED", "DECLINED", "OPTED_OUT",
                } or row["campaign"].get("provider_message_id")
            ))
            failed_count = max(failed_count, sum(
                1 for row in campaign_rows
                if row["campaign"].get("status") == "FAILED"
                or str(row["campaign"].get("provider_status") or "").lower() == "failed"
            ))
            uncertain_count = max(uncertain_count, sum(
                1 for row in campaign_rows
                if row["campaign"].get("status") in {"SENDING", "DELIVERY_UNKNOWN"}
            ))
            delivered_count = max(delivered_count, sum(
                1 for row in campaign_rows
                if str(row["campaign"].get("provider_status") or "").lower()
                in {"delivered", "read", "seen", "played"}
                or row["campaign"].get("status") in {"DELIVERED", "READ"}
            ))
            read_count = max(read_count, sum(
                1 for row in campaign_rows
                if str(row["campaign"].get("provider_status") or "").lower()
                in {"read", "seen", "played"} or row["campaign"].get("status") == "READ"
            ))
            response_count = max(response_count, sum(
                int(row["campaign"].get("reply_count") or 0) for row in campaign_rows
            ))
            pending_total = pending_campaign_count()
            run_display_limit = progress_total_limit or display_limit
            pending_batch = max(
                min(run_display_limit, batch_pending_initial + progress_offset)
                - progress_offset - attempts, 0,
            )
            progress_record = {
                "run_id": run_id, "at": now_fn(), "campaign_id": campaign_id,
                "property_code": str(entry["campaign"].get("property_code") or ""),
                "attempts": attempts + progress_offset,
                "attempts_in_campaign": attempts, "max_messages": run_display_limit,
                "provider_accepted": accepted_count, "provider_failed": failed_count,
                "delivery_uncertain": uncertain_count, "delivered_verified": delivered_count,
                "read_verified": read_count, "responses": response_count,
                "pending_in_batch": pending_batch, "pending_total": pending_total,
                "last_status": status, "provider_status": provider_status,
                "next_attempt_at": None,
            }
            _append_progress(progress_file, progress_record)
            emit_progress(_progress_line(
                attempts=attempts + progress_offset, max_messages=run_display_limit, accepted=accepted_count,
                failed=failed_count, uncertain=uncertain_count,
                delivered=delivered_count, read=read_count, responses=response_count,
                pending_batch=pending_batch, pending_total=pending_total,
                property_code=progress_record["property_code"], status=status,
                next_attempt=None, pause_seconds=None, timezone_name=timezone_name,
            ))
            if status in {"DELIVERY_UNKNOWN", "HISTORY_RECONCILIATION_REQUIRED",
                          "SENDING", "ALREADY_ATTEMPTED"}:
                print()
                return {"status": "STOPPED_FOR_RECONCILIATION", "attempts": attempts,
                        "pending": max(len(entries)-len(results), 0), "results": results,
                        "progress_file": str(progress_file),
                        "accepted": accepted_count, "failed": failed_count,
                        "uncertain": uncertain_count}
            if status == "SENT":
                remaining = len(entries) - index - 1
                if remaining == 0 and pause_after_last:
                    remaining = 1
                if remaining and attempts < run_limit:
                    pause = rng(min_delay_seconds, max_delay_seconds)
                    wake = now_fn() + timedelta(seconds=pause)
                    if not _within_hours(wake, config):
                        print()
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
                        progress_record["next_attempt_at"] = wake
                        _append_progress(progress_file, progress_record)
                        emit_progress(_progress_line(
                            attempts=attempts + progress_offset, max_messages=run_display_limit, accepted=accepted_count,
                            failed=failed_count, uncertain=uncertain_count,
                            delivered=delivered_count, read=read_count, responses=response_count,
                            pending_batch=pending_batch, pending_total=pending_total,
                            property_code=progress_record["property_code"], status=status,
                            next_attempt=wake, pause_seconds=pause, timezone_name=timezone_name,
                        ))
                        sleep_fn(pause)
                    except KeyboardInterrupt:
                        print()
                        return {"status": "INTERRUPTED_DURING_PAUSE", "attempts": attempts,
                                "pending": remaining, "results": results,
                                "progress_file": str(progress_file),
                                "accepted": accepted_count, "failed": failed_count,
                                "uncertain": uncertain_count}
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
        "accepted": accepted_count, "failed": failed_count,
        "uncertain": uncertain_count, "delivered_verified": delivered_count,
        "read_verified": read_count, "responses": response_count,
        "daily_attempts_before": daily_before, "daily_provider_cap": daily_provider_cap,
        "daily_attempts_after": daily_after, "pending": pending, "results": results,
        "progress_file": str(progress_file),
    }


def prepare_selected(db: Any, *, event: PriceDropEvent, lead_ids: list[str],
                     operator: str, contact_basis_reviewed: bool,
                     whatsapp_contact_reviewed: bool = False,
                     whatsapp_contact_evidence: str | dict[str, str] | None = None,
                     temperature_filter: str = "ALL",
                     lead_max_age_days: int | None = DEFAULT_LEAD_MAX_AGE_DAYS,
                     temperature_reviews: dict[str, dict[str, Any]] | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
    if not operator.strip():
        raise CliError("operator_required")
    if not contact_basis_reviewed:
        raise CliError("contact_basis_must_be_reviewed")
    if not lead_ids:
        raise CliError("at_least_one_lead_id_required")
    selected_ids = list(dict.fromkeys(str(value) for value in lead_ids))
    reviews = temperature_reviews or {}
    stamp = now or datetime.now(timezone.utc)
    candidates = {row["lead_id"]: row for row in find_candidates_for_event(
        db, event, temperature_filter=temperature_filter,
        lead_max_age_days=lead_max_age_days,
        temperature_reviews=reviews, now=stamp,
    )}
    plans = []
    missing_authorization = []
    for lead_id in selected_ids:
        candidate = candidates.get(lead_id)
        if not candidate or not candidate["eligible"]:
            raise CliError(f"selected_lead_not_eligible:{lead_id[-6:]}")
        lead = db[LEADS_COLLECTION].find_one({"_id": _lead_id(lead_id)}) or {}
        phone = lead.get("contact_phone_normalized") or lead.get("phone") or ""
        authorization = v2.reusable_whatsapp_contact_authorization(
            lead, phone=str(phone), now=stamp,
        )
        if authorization is None:
            missing_authorization.append(lead_id)
        plans.append({"lead_id": lead_id, "candidate": candidate, "lead": lead,
                      "reusable_authorization": authorization})
    evidence_by_lead: dict[str, str] = {}
    if isinstance(whatsapp_contact_evidence, dict):
        evidence_by_lead = {str(key): str(value or "").strip()
                            for key, value in whatsapp_contact_evidence.items()}
    elif whatsapp_contact_evidence is not None:
        evidence = str(whatsapp_contact_evidence or "").strip()
        if len(missing_authorization) > 1:
            raise CliError("whatsapp_contact_authorization_evidence_required_per_lead")
        if missing_authorization:
            evidence_by_lead[missing_authorization[0]] = evidence
    if missing_authorization and not whatsapp_contact_reviewed:
        raise CliError("whatsapp_contact_authorization_review_required")
    if any(len(evidence_by_lead.get(value, "")) < 8
           for value in missing_authorization):
        raise CliError("whatsapp_contact_authorization_evidence_required_per_lead")
    output = []
    reused_count = 0
    reviewed_count = 0
    for plan in plans:
        lead_id = plan["lead_id"]
        candidate = plan["candidate"]
        lead = plan["lead"]
        history = _history_steps(db, event, candidate["last_notice"].get("sent_at")
                                 if candidate["last_notice"] else None)
        variant_id = message_variant_id(str(lead_id), event.event_id)
        body = event_message(
            event, last_notice=candidate["last_notice"], history=history,
            recipient_name=candidate["first_name"], variant_id=variant_id,
        )
        temperature_review = candidate.get("temperature_review")
        if temperature_review:
            temperature_review = {
                **temperature_review, "reviewed_by": operator[:80],
            }
            if (str(temperature_review.get("reviewed_temperature") or "").upper()
                    not in {"HOT", "NORMAL", "COLD"}
                    or len(str(temperature_review.get("evidence") or "").strip()) < 8):
                raise CliError("temperature_review_classification_and_evidence_required")
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
            "campaign_filters_enabled": True,
            "temperature_filter": normalize_temperature_filter(temperature_filter),
            "temperature_review": temperature_review,
            "lead_max_age_days": normalize_lead_max_age_days(lead_max_age_days),
            "lead_age_days_snapshot": candidate["lead_age_days"],
            "message_variant_id": variant_id,
            "now": stamp,
        }
        prepared = v2.prepare_one(db, **data)
        if plan["reusable_authorization"] is not None:
            reused_count += 1
        else:
            v2.review_whatsapp_contact(
                db, lead_id=lead_id, campaign_id=event.campaign_id,
                operator=operator, evidence=evidence_by_lead[lead_id], now=stamp,
            )
            reviewed_count += 1
        output.append(prepared)
    return {"status": "PREPARED", "campaign_id": event.campaign_id,
            "prepared_count": len(output),
            "lead_last6": [str(row["lead_id"])[-6:] for row in output],
            "whatsapp_authorizations_reused": reused_count,
            "whatsapp_authorizations_reviewed": reviewed_count}


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
    unique_ids = list(dict.fromkeys(str(value) for value in lead_ids))
    if not operator.strip() or len(evidence.strip()) < 8 or len(unique_ids) != 1:
        raise CliError("one_lead_operator_and_whatsapp_evidence_required")
    output = [v2.review_whatsapp_contact(
        db, lead_id=unique_ids[0], campaign_id=campaign_id,
        operator=operator, evidence=evidence,
    )]
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
                  batch_size: int = 500, temperature_filter: str = "ALL",
                  lead_max_age_days: int | None = DEFAULT_LEAD_MAX_AGE_DAYS,
                  now: datetime | None = None) -> int:
    events = _events(db, property_code, since=since, currency=currency,
                     operation=operation, limit=limit, batch_size=batch_size)
    if not events:
        print("No hay rebajas vigentes que cumplan los filtros.")
        return 0
    print("Propiedad | Tipo | Comuna | Precio anterior → vigente | Cambio | Fecha (Chile) | Leads elegibles")
    for event in events:
        candidates = find_candidates_for_event(
            db, event, temperature_filter=temperature_filter,
            lead_max_age_days=lead_max_age_days, now=now,
        )
        eligible = sum(1 for candidate in candidates if candidate["eligible"])
        age_excluded = sum(bool(candidate["excluded_by_age"]) for candidate in candidates)
        pending_temp = sum(bool(candidate["temperature_review_required"]) for candidate in candidates)
        date_review = sum(bool(candidate["age_review_required"]) for candidate in candidates)
        already_notified = sum(
            1 for candidate in candidates
            if any(item.get("campaign_id") == event.event_id
                   and item.get("status") in {"SENT", "RESPONDED"}
                   for item in candidate["campaign_history"])
        )
        print(_display(event, eligible))
        print(f"  candidatos={len(candidates)} | elegibles={eligible} | ya_notificados={already_notified} | "
              f"excluidos_solo_por_antiguedad={age_excluded} | "
              f"temperatura_revision={pending_temp} | fecha_revision={date_review}")
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
                f"{row['temperature']} | edad={row['lead_age_days'] if row['lead_age_days'] is not None else 'REVISAR'} días | "
                f"created_at={row['created_at'] or 'N/A'} | consulta={inquiry} | "
                f"evidencia={row['inquiry_reference'] or 'N/A'} | "
                f"historial={history_rows} | nombre={row['first_name'] or 'saludo genérico'} | "
                f"último aviso={row['last_notice'] or 'N/A'} | "
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
    variant_id = message_variant_id(candidate["lead_id"], event.event_id)
    return {
        "event": event,
        "candidate": candidate,
        "variant_id": variant_id,
        "message": event_message(
            event, last_notice=candidate["last_notice"], history=history,
            recipient_name=candidate["first_name"], variant_id=variant_id,
        ),
    }


def deduplicate_wave_recipients(
    selected_by_event: dict[str, tuple[PriceDropEvent, list[dict[str, Any]]]],
) -> tuple[dict[str, tuple[PriceDropEvent, list[dict[str, Any]]]], int]:
    """Never contact a lead twice in the same operator-approved wave.

    Prioritize the newest verified price drop, then greatest percentage
    reduction. A subsequent separately approved wave is unaffected.
    """
    preferred: dict[str, tuple[tuple[Any, ...], str]] = {}
    total_selected = 0
    for event_id, (event, rows) in selected_by_event.items():
        for candidate in rows:
            total_selected += 1
            lid = str(candidate["lead_id"])
            order = (event.dropped_at, event.percent_reduction, event.event_id)
            if lid not in preferred or order > preferred[lid][0]:
                preferred[lid] = (order, event_id)

    result = {}
    for event_id, (event, rows) in selected_by_event.items():
        kept = [row for row in rows
                if preferred[str(row["lead_id"])][1] == event_id]
        if kept:
            result[event_id] = (event, kept)
    return result, total_selected - len(preferred)


def _approved_campaign_batch(
    db: Any, *, campaign_id: str | None = None, include_pilot: bool = False,
) -> dict[str, set[str]]:
    """Select approved pending rows; sent rows are never included for retries."""
    groups = _campaigns_by_status(db, ("APPROVED", "SENDING", "DELIVERY_UNKNOWN"))
    if campaign_id:
        groups = {campaign_id: groups.get(campaign_id, [])}
    result: dict[str, set[str]] = {}
    owner_by_lead: dict[str, str] = {}
    uncertain_leads = {
        str(row["lead"].get("_id"))
        for rows in groups.values() for row in rows
        if row["campaign"].get("status") in {"SENDING", "DELIVERY_UNKNOWN"}
    }
    for cid, rows in sorted(groups.items()):
        if cid == PILOT_CAMPAIGN_ID and not include_pilot and campaign_id is None:
            continue
        ids = set()
        for row in rows:
            if row["campaign"].get("status") != "APPROVED":
                continue
            lead_id = str(row["lead"].get("_id"))
            if lead_id in uncertain_leads:
                raise CliError("lead_has_uncertain_delivery_reconcile_before_new_attempt")
            prior = owner_by_lead.get(lead_id)
            if prior and prior != cid:
                raise CliError("lead_has_multiple_approved_campaigns_review_before_send")
            owner_by_lead[lead_id] = cid
            ids.add(lead_id)
        if ids:
            result[cid] = ids
    return result


def execute_approved_batch(
    db: Any, *, campaigns: dict[str, set[str]] | None = None,
    campaign_id: str | None = None, include_pilot: bool = False,
    max_messages: int = 20, min_delay_seconds: float = 300,
    max_delay_seconds: float = 600,
    start_time: day_time = day_time(9), end_time: day_time = day_time(19),
    allowed_weekdays: str = "MON,TUE,WED,THU,FRI",
    timezone_name: str = "America/Santiago", provider_plan: str = "trial",
    daily_provider_cap: int = 50, temperature_filter: str | None = None,
    lead_max_age_days: int | None = None, enforce_campaign_filters: bool = False,
    already_confirmed: bool = False,
    confirm: Callable[[str], str] = input,
    sleep_fn: Callable[[float], None] = time.sleep,
    sender: Callable[[str, str], dict] | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    provider_ready: Callable[[], bool] = _provider_ready,
    usage_reader: Callable[[datetime], dict[str, Any] | int] | None = None,
    journal_path: str | Path | None = None,
    progress_path: str | Path | None = None,
    publication_verifier: Callable[..., Any] = verify_current_listing,
    progress_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Shared paced sender for pilot rows, resumed batches and portfolio campaigns."""
    validate_limits(max_messages, min_delay_seconds, max_delay_seconds,
                    provider_plan=provider_plan, daily_cap=daily_provider_cap)
    selected = campaigns if campaigns is not None else _approved_campaign_batch(
        db, campaign_id=campaign_id, include_pilot=include_pilot,
    )
    selected = {cid: set(ids) for cid, ids in selected.items() if ids}
    if not selected:
        return {"status": "NO_APPROVED_RECIPIENTS", "attempts": 0, "pending": 0}

    approved_rows: dict[str, list[dict[str, Any]]] = {}
    snapshots: dict[str, dict[str, str]] = {}
    expected_by_campaign: dict[str, dict[str, dict[str, Any]]] = {}
    publication_checks: dict[str, Any] = {}
    seen_leads: dict[str, str] = {}
    for cid, ids in selected.items():
        rows = _entries(db, cid, ("APPROVED",), ids)
        if len(rows) != len(ids):
            raise CliError("approved_campaign_rows_changed_before_execution")
        approved_rows[cid] = rows
        snapshots[cid] = {
            str(row["lead"]["_id"]): str(row["campaign"].get("outbound_message") or "")
            for row in rows
        }
        expected_by_campaign[cid] = {}
        for row in rows:
            lead_id = str(row["lead"]["_id"])
            if lead_id in seen_leads and seen_leads[lead_id] != cid:
                raise CliError("lead_has_multiple_approved_campaigns_review_before_send")
            seen_leads[lead_id] = cid
            campaign = row["campaign"]
            code = str(campaign.get("property_code") or "")
            expected = _expected_publication_from_campaign(campaign)
            expected_by_campaign[cid][code] = expected
            key = f"{cid}:{code}"
            if key not in publication_checks:
                publication_checks[key] = _publication_result_dict(publication_verifier(
                    db, property_code=code, currency=expected["currency"],
                    expected_price=expected["price"], expected_url=expected["url"],
                    now=now_fn(),
                ))
    print(json.dumps({"publication_checks": publication_checks},
                     ensure_ascii=False, default=str))
    blocked = next((row for row in publication_checks.values()
                    if not row.get("sendable")), None)
    if blocked:
        return {"status": "BLOCKED_PUBLICATION_REVIEW", "attempts": 0,
                "pending": sum(map(len, selected.values())),
                "publication_checks": publication_checks}

    total_selected = sum(map(len, selected.values()))
    print(f"PROCASA — CAMPAÑA DE REBAJAS | aprobados pendientes: {total_selected} | "
          f"máximo de intentos en esta ejecución: {max_messages}")
    print("Vista previa exacta de los registros aprobados:")
    for index, (cid, rows) in enumerate(
            ((cid, row) for cid in sorted(approved_rows) for row in approved_rows[cid]), 1):
        print(f"\n[{index}] campaña {cid} | propiedad {rows['campaign'].get('property_code')} | "
              f"teléfono ***{_phone_last4(rows['lead'])}")
        print(rows["campaign"].get("outbound_message") or "[mensaje faltante]")
    if not already_confirmed:
        expected_token = f"ENVIAR {total_selected}"
        if confirm(f"\nEscribe exactamente {expected_token} para iniciar el lote: ").strip() != expected_token:
            return {"status": "CANCELLED", "attempts": 0, "pending": total_selected}

    total_attempts = 0
    outcomes = []
    campaign_items = list(sorted(selected.items()))
    all_pending = {cid: set(ids) for cid, ids in campaign_items}
    for position, (cid, ids) in enumerate(campaign_items):
        remaining = max_messages - total_attempts
        if remaining <= 0:
            break
        outcome = execute_campaign(
            db, campaign_id=cid, max_messages=remaining,
            min_delay_seconds=min_delay_seconds, max_delay_seconds=max_delay_seconds,
            start_time=start_time, end_time=end_time,
            allowed_weekdays=allowed_weekdays, timezone_name=timezone_name,
            provider_plan=provider_plan, daily_provider_cap=daily_provider_cap,
            live_links_verified=True,
            link_verifier=lambda code, stamp, cid=cid: _publication_result_dict(
                publication_verifier(
                    db, property_code=code,
                    currency=expected_by_campaign[cid][code]["currency"],
                    expected_price=expected_by_campaign[cid][code]["price"],
                    expected_url=expected_by_campaign[cid][code]["url"], now=stamp,
                )),
            lead_allowlist=ids, expected_messages=snapshots[cid],
            pending_campaigns=all_pending,
            temperature_filter=temperature_filter,
            lead_max_age_days=lead_max_age_days,
            enforce_campaign_filters=enforce_campaign_filters,
            confirm=lambda _prompt, selected_id=cid: f"ENVIAR {selected_id}",
            sleep_fn=sleep_fn, sender=sender, now_fn=now_fn,
            provider_ready=provider_ready, usage_reader=usage_reader,
            journal_path=journal_path,
            progress_path=progress_path,
            progress_offset=total_attempts, progress_total_limit=max_messages,
            display_max_messages=max_messages,
            pause_after_last=position < len(campaign_items) - 1,
            progress_fn=progress_fn,
        )
        outcomes.append({"campaign_id": cid, **outcome})
        total_attempts += int(outcome.get("attempts") or 0)
        if outcome.get("status") not in {"COMPLETED", "LIMIT_REACHED", "NO_APPROVED_RECIPIENTS"}:
            pending = sum(len(_entries(db, item_id, ("APPROVED",), item_ids))
                          for item_id, item_ids in campaign_items)
            return {"status": outcome.get("status"), "attempts": total_attempts,
                    "pending": pending, "campaign_results": outcomes,
                    "publication_checks": publication_checks}
    pending = sum(len(_entries(db, cid, ("APPROVED",), ids))
                  for cid, ids in campaign_items)
    return {"status": "LIMIT_REACHED" if pending else "COMPLETED",
            "attempts": total_attempts, "pending": pending,
            "campaign_results": outcomes,
            "publication_checks": publication_checks}



def interactive_workflow(
    db: Any, *, input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    sender: Callable[[str, str], dict] | None = None,
    usage_reader: Callable[[datetime], dict[str, Any] | int] | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    since: datetime | None = None, property_code: str | None = None,
    currency: str | None = None, operation: str | None = None,
    temperature_filter: str | None = None,
    lead_max_age_days: int | None = None,
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
    if temperature_filter is None:
        raw_temperature_filter = input_fn(
            "Temperatura [1 HOT | 2 NORMAL | 3 COLD | 4 HOT+NORMAL | 5 TODOS] (Enter = 5): "
        ).strip() or "ALL"
        temperature_filter = _temperature_menu_value(raw_temperature_filter)
    else:
        temperature_filter = normalize_temperature_filter(temperature_filter)
    if lead_max_age_days is None:
        raw_age = input_fn(
            f"Antigüedad máxima en días [predeterminado {DEFAULT_LEAD_MAX_AGE_DAYS}; 0 = sin límite]: "
        ).strip()
        lead_max_age_days = (DEFAULT_LEAD_MAX_AGE_DAYS if not raw_age else
                             normalize_lead_max_age_days(raw_age))
    else:
        lead_max_age_days = normalize_lead_max_age_days(lead_max_age_days)
    now = now_fn()
    try:
        pending_batch = _approved_campaign_batch(db)
    except CliError as exc:
        output_fn(f"No se puede reanudar el lote: {exc}")
        return {"status": "BLOCKED", "reason": str(exc), "attempts": 0}
    pending_count = sum(map(len, pending_batch.values()))
    if pending_count:
        output_fn(f"Hay {pending_count} mensajes aprobados pendientes de campañas anteriores.")
        if input_fn(
                f"Escribe REANUDAR {pending_count} para revisar y continuar ese lote: ").strip() \
                != f"REANUDAR {pending_count}":
            return {"status": "APPROVED_PENDING_SEND", "prepared": 0,
                    "attempts": 0, "pending": pending_count}
        try:
            batch_limit = int(input_fn(
                f"Máximo de intentos en esta ejecución [predeterminado {max_messages_default}]: "
            ).strip() or max_messages_default)
            batch_min = float(input_fn(
                f"Pausa mínima en segundos [predeterminado {min_delay_default:g}]: "
            ).strip() or min_delay_default)
            batch_max = float(input_fn(
                f"Pausa máxima en segundos [predeterminado {max_delay_default:g}]: "
            ).strip() or max_delay_default)
        except ValueError as exc:
            raise CliError("message_limit_and_delays_must_be_numbers") from exc
        if batch_limit > max_messages_default:
            raise CliError("max_messages_exceeds_command_line_ceiling")
        return execute_approved_batch(
            db, campaigns=pending_batch, max_messages=batch_limit,
            min_delay_seconds=batch_min, max_delay_seconds=batch_max,
            start_time=start_time, end_time=end_time,
            allowed_weekdays=allowed_weekdays, timezone_name=timezone_name,
            provider_plan=provider_plan, daily_provider_cap=daily_provider_cap,
            temperature_filter=temperature_filter, lead_max_age_days=lead_max_age_days,
            enforce_campaign_filters=True, confirm=input_fn, sleep_fn=sleep_fn,
            sender=sender, now_fn=now_fn, provider_ready=provider_ready,
            usage_reader=usage_reader, journal_path=journal_path,
            publication_verifier=publication_verifier, progress_fn=output_fn,
        )
    events = _events(db, property_code, since=since, currency=currency,
                     operation=operation, limit=history_limit, batch_size=batch_size)
    if not events:
        output_fn("No hay rebajas vigentes para los filtros seleccionados.")
        return {"status": "NO_EVENTS", "prepared": 0, "attempts": 0}

    output_fn("Rebajas vigentes y consultas históricas verificadas (barrido en lotes):")
    output_fn(f"Temperatura seleccionada: {temperature_filter} | antigüedad máxima: "
              f"{lead_max_age_days if lead_max_age_days is not None else 'sin límite'} días")
    discovered_stats = {"candidates": 0, "eligible": 0, "excluded": 0,
                        "already_notified": 0, "temperature_review": 0,
                        "age_review": 0, "age_excluded": 0}
    for index, event in enumerate(events, 1):
        rows = find_candidates_for_event(
            db, event, temperature_filter=temperature_filter,
            lead_max_age_days=lead_max_age_days, now=now,
        )
        eligible = sum(bool(row["eligible"]) for row in rows)
        age_excluded = sum(bool(row["excluded_by_age"]) for row in rows)
        notified = sum(1 for row in rows if any(
            item.get("campaign_id") == event.event_id
            and item.get("status") in {"SENT", "RESPONDED"}
            for item in row["campaign_history"]
        ))
        discovered_stats["candidates"] += len(rows)
        discovered_stats["eligible"] += eligible
        discovered_stats["excluded"] += len(rows) - eligible
        discovered_stats["already_notified"] += notified
        discovered_stats["temperature_review"] += sum(bool(row["temperature_review_required"]) for row in rows)
        discovered_stats["age_review"] += sum(bool(row["age_review_required"]) for row in rows)
        discovered_stats["age_excluded"] += age_excluded
        output_fn(
            f"  {index}. {_display(event, eligible)} | candidatos={len(rows)} | "
            f"elegibles={eligible} | excluidos={len(rows)-eligible} | "
            f"ya_notificados={notified} | excluidos_solo_por_antigüedad={age_excluded}"
        )
    output_fn(
        f"Totales: propiedades={len(events)} | consultas previas={discovered_stats['candidates']} | "
        f"elegibles={discovered_stats['eligible']} | excluidos={discovered_stats['excluded']} | "
        f"ya_notificados={discovered_stats['already_notified']} | "
        f"revision_temperatura={discovered_stats['temperature_review']} | "
        f"revision_fecha={discovered_stats['age_review']} | "
        f"excluidos_solo_por_antigüedad={discovered_stats['age_excluded']}"
    )
    chosen_events = [events[index - 1] for index in _menu_selection(
        input_fn("Elige propiedades/eventos por número (ej. 1,3 o all): "), len(events),
    )]

    selected_by_event: dict[str, tuple[PriceDropEvent, list[dict[str, Any]]]] = {}
    for event in chosen_events:
        rows = find_candidates_for_event(
            db, event, temperature_filter=temperature_filter,
            lead_max_age_days=lead_max_age_days, now=now,
        )
        output_fn(f"\nPropiedad {event.property_code}: leads elegibles, excluidos y motivo")
        for index, row in enumerate(rows, 1):
            marker = "ELEGIBLE" if row["eligible"] else "EXCLUIDO"
            inquiry = row["inquiry_at"].astimezone(CHILE_TZ).isoformat() if row["inquiry_at"] else "N/A"
            output_fn(
                f"  {index}. {marker} | teléfono ***{row['phone_last4']} | "
                f"{row['temperature']} | edad={row['lead_age_days'] if row['lead_age_days'] is not None else 'REVISAR'} días | "
                f"created_at={row['created_at'] or 'N/A'} | consulta={inquiry} | "
                f"evidencia={row['inquiry_reference'] or 'N/A'} | motivo={row['reason']}"
            )
        pending_indexes = [
            index for index, row in enumerate(rows, 1)
            if row["base_eligible"] and row["temperature_review_required"]
            and not row["age_review_required"] and not row["excluded_by_age"]
        ]
        temperature_reviews = {}
        if pending_indexes:
            output_fn("Temperaturas pendientes requieren revisión manual; no se enviarán sin esa revisión.")
            raw_pending = input_fn(
                "Si revisaste CRM, selecciona números pendientes para clasificarlos manualmente (Enter = ninguno): "
            ).strip()
            if raw_pending:
                selected_pending = _menu_selection(raw_pending, len(rows))
                if any(index not in set(pending_indexes) for index in selected_pending):
                    raise CliError("manual_temperature_review_only_accepts_pending_candidates")
                for index in selected_pending:
                    candidate = rows[index - 1]
                    reviewed_temperature = input_fn(
                        f"Clasificación revisada para …{candidate['lead_id'][-6:]} "
                        "(HOT/NORMAL/COLD; no modifica CRM): "
                    ).strip().upper()
                    if reviewed_temperature not in {"HOT", "NORMAL", "COLD"}:
                        raise CliError("manual_temperature_review_must_be_HOT_NORMAL_or_COLD")
                    if not temperature_filter_matches(reviewed_temperature, temperature_filter):
                        raise CliError("manual_temperature_classification_outside_selected_filter")
                    evidence = input_fn(
                        f"Referencia CRM de la revisión manual para …{candidate['lead_id'][-6:]}: "
                    ).strip()
                    if len(evidence) < 8:
                        raise CliError("temperature_review_classification_and_evidence_required")
                    temperature_reviews[candidate["lead_id"]] = {
                        "reviewed_temperature": reviewed_temperature,
                        "evidence": evidence,
                        "reviewed_at": now.isoformat(),
                    }
                rows = find_candidates_for_event(
                    db, event, temperature_filter=temperature_filter,
                    lead_max_age_days=lead_max_age_days,
                    temperature_reviews=temperature_reviews, now=now,
                )
                output_fn("Resultados después de la revisión manual de temperatura:")
                for index, row in enumerate(rows, 1):
                    output_fn(
                        f"  {index}. {'ELEGIBLE' if row['eligible'] else 'EXCLUIDO'} | "
                        f"teléfono ***{row['phone_last4']} | {row['temperature']}"
                        + (f" → {row['effective_temperature']} (revisión manual)"
                           if row.get("temperature_review") else "")
                        + f" | motivo={row['reason']}"
                    )
        eligible_indexes = [index for index, row in enumerate(rows, 1) if row["eligible"]]
        if not eligible_indexes:
            output_fn("  No hay destinatarios elegibles para esta rebaja.")
            continue
        raw = input_fn("Selecciona destinatarios elegibles por número (ej. 1,2): ").strip()
        selected_indexes = _menu_selection(raw, len(rows))
        if any(index not in set(eligible_indexes) for index in selected_indexes):
            raise CliError("selected_candidate_is_excluded_review_reason")
        selected = [rows[index - 1] for index in selected_indexes]
        for candidate in selected:
            candidate["temperature_review"] = temperature_reviews.get(candidate["lead_id"])
        selected_by_event[event.event_id] = (event, selected)
    if not selected_by_event:
        return {"status": "NO_SELECTED_RECIPIENTS", "prepared": 0, "attempts": 0}

    selected_by_event, overlap_removed = deduplicate_wave_recipients(selected_by_event)
    if overlap_removed:
        output_fn(
            f"Control de duplicados: se descartaron {overlap_removed} selecciones "
            "repetidas de un mismo lead entre propiedades. Se conserva la "
            "rebaja más reciente (mayor rebaja porcentual en caso de empate)."
        )
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
    if max_messages > max_messages_default:
        raise CliError("max_messages_exceeds_command_line_ceiling")
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
            f"nombre={candidate['first_name'] or 'saludo genérico'} | variante={preview['variant_id']} | "
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
    reusable_authorizations = set()
    for event, rows in selected_by_event.values():
        for candidate in rows:
            lead = db[LEADS_COLLECTION].find_one(
                {"_id": _lead_id(candidate["lead_id"])},
                {"_id": 1, "phone": 1, "contact_phone_normalized": 1,
                 "price_drop_v2.whatsapp_contact_authorization": 1,
                 "price_drop_v2.opted_out": 1, "price_drop_v2.campaigns": 1,
                 "messages": 1, "conversation_status": 1,
                 "conversation_status_reason": 1, "conversation_status_evidence": 1},
            ) or {}
            authorization = v2.reusable_whatsapp_contact_authorization(
                lead,
                phone=str(lead.get("contact_phone_normalized") or lead.get("phone") or ""),
                now=now,
            )
            if authorization is not None:
                reusable_authorizations.add(candidate["lead_id"])
                output_fn(
                    f"Autorización WhatsApp reutilizable verificada para "
                    f"propiedad {event.property_code}, teléfono ***{candidate['phone_last4']}."
                )
                continue
            evidence_by_lead[candidate["lead_id"]] = input_fn(
                f"Referencia CRM de autorización WhatsApp para avisos de rebaja "
                f"(propiedad {event.property_code}, teléfono ***{candidate['phone_last4']}): "
            ).strip()
    missing_authorizations = [
        row["lead_id"] for _, rows in selected_by_event.values() for row in rows
        if row["lead_id"] not in reusable_authorizations
    ]
    if any(len(evidence_by_lead.get(value, "")) < 8 for value in missing_authorizations):
        raise CliError("whatsapp_contact_authorization_evidence_required_per_lead")
    if input_fn(
        f"Escribe PREPARAR {selected_count} para crear campañas PREPARED. "
        "Confirmas los permisos CRM indicados y el uso de los permisos reutilizables: "
    ).strip() \
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
            contact_basis_reviewed=True,
            whatsapp_contact_reviewed=any(value in evidence_by_lead for value in ids),
            whatsapp_contact_evidence=evidence_by_lead,
            temperature_filter=temperature_filter,
            lead_max_age_days=lead_max_age_days,
            temperature_reviews={
                row["lead_id"]: row["temperature_review"]
                for row in rows if row.get("temperature_review")
            },
            now=now,
        )
    output_fn("Campañas PREPARED. Mensajes exactos leídos desde los registros preparados:")
    for event_id, ids in lead_ids_by_event.items():
        rows = _entries(db, event_id, ("PREPARED",), set(ids))
        if len(rows) != len(ids):
            return {"status": "BLOCKED_PREPARE_READBACK_MISMATCH", "attempts": 0}
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
    output_fn(
        f"PROCASA — CAMPAÑA DE REBAJAS\nTemperatura seleccionada: {temperature_filter}\n"
        f"Antigüedad máxima: {lead_max_age_days if lead_max_age_days is not None else 'sin límite'} días\n"
        f"Aprobados pendientes de envío: {selected_count}\nMáximo por ejecución: {max_messages}\n"
        f"Pendientes estimados después del lote: {max(selected_count - max_messages, 0)}"
    )
    if input_fn(f"Escribe ENVIAR {selected_count} para iniciar los intentos aprobados: ").strip() \
            != f"ENVIAR {selected_count}":
        return {"status": "APPROVED_PENDING_SEND", "prepared": selected_count,
                "attempts": 0, "campaign_ids": list(lead_ids_by_event),
                "backup": str(approval_backup)}

    outcome = execute_approved_batch(
        db, campaigns={cid: set(ids) for cid, ids in lead_ids_by_event.items()},
        max_messages=max_messages, min_delay_seconds=min_delay,
        max_delay_seconds=max_delay, start_time=start_time, end_time=end_time,
        allowed_weekdays=allowed_weekdays, timezone_name=timezone_name,
        provider_plan=provider_plan, daily_provider_cap=daily_provider_cap,
        temperature_filter=temperature_filter, lead_max_age_days=lead_max_age_days,
        enforce_campaign_filters=True, already_confirmed=True,
        sender=sender, usage_reader=usage_reader, now_fn=now_fn,
        provider_ready=provider_ready, sleep_fn=sleep_fn,
        journal_path=journal_path, publication_verifier=publication_verifier,
        progress_fn=output_fn,
    )
    return {**outcome, "prepared": selected_count, "backup": str(approval_backup)}


def _report(db: Any, campaign_id: str | None = None) -> int:
    groups = _campaigns_by_status(db, (
        "PREPARED", "APPROVED", "SENDING", "SENT", "FAILED", "DELIVERY_UNKNOWN",
        "RESPONDED", "DECLINED", "OPTED_OUT", "BLOCKED",
        "SUPERSEDED",
    ))
    selected = ({campaign_id: groups.get(campaign_id, [])} if campaign_id else groups)
    for name in sorted(selected):
        summary = _status_report(selected[name])
        print(json.dumps(summary, ensure_ascii=False, default=str))
    if not selected:
        print("No hay campañas registradas.")
    return 0


def _campaign_stamp(campaign: dict[str, Any]) -> datetime | None:
    for key in ("sent_at", "attempted_at", "prepared_at", "price_drop_at"):
        raw = campaign.get(key)
        if isinstance(raw, datetime) and raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        stamp = parse_aware_datetime(raw)
        if stamp is not None:
            return stamp
    return None


def _summary_report(db: Any, *, since: datetime | None = None,
                    period_days: int | None = None,
                    temperature_filter: str = "ALL",
                    lead_max_age_days: int | None = DEFAULT_LEAD_MAX_AGE_DAYS,
                    now: datetime | None = None) -> dict[str, Any]:
    """Read-only, event-aware summary with unique lead counts per event."""
    stamp = now or datetime.now(timezone.utc)
    selected_temperature = normalize_temperature_filter(temperature_filter)
    age_limit = normalize_lead_max_age_days(lead_max_age_days)
    if period_days is not None:
        if period_days < 1:
            raise CliError("period_days_must_be_positive")
        local_cutoff = stamp.astimezone(CHILE_TZ) - timedelta(days=period_days)
        since = local_cutoff.astimezone(timezone.utc)

    # Current opportunities are displayed even when their drop predates the
    # selected activity period; the period filter applies to campaign records.
    current_events = _events(db, limit=None, batch_size=500)
    current_by_code = {event.property_code: event for event in current_events}
    candidate_counts: dict[str, int] = {}
    candidate_total = 0
    age_excluded = 0
    for event in current_events:
        candidates = find_candidates_for_event(
            db, event, temperature_filter=selected_temperature,
            lead_max_age_days=age_limit, now=stamp,
        )
        count = sum(bool(row["eligible"]) for row in candidates)
        candidate_counts[event.event_id] = count
        candidate_total += count
        age_excluded += sum(bool(row["excluded_by_age"]) for row in candidates)

    status_set = (
        "PREPARED", "APPROVED", "SENDING", "SENT", "FAILED", "DELIVERED", "READ",
        "DELIVERY_UNKNOWN", "RESPONDED", "DECLINED", "OPTED_OUT", "BLOCKED",
        "SUPERSEDED", "STALE_PRICE",
    )
    by_event: dict[str, dict[str, Any]] = {}
    global_sets: dict[str, set[tuple[str, str]]] = {
        key: set() for key in (
            "accepted", "delivered", "read", "responses", "interested", "visits",
            "offers", "calls", "alternatives", "optouts", "search_complete",
            "uncertain", "failed", "prepared", "approved", "pending",
        )
    }
    latest_stamp = None
    docs = db[LEADS_COLLECTION].find(
        {"price_drop_v2.campaigns.status": {"$in": list(status_set)}},
        {"_id": 1, "phone": 1, "contact_phone_normalized": 1,
         "lead_temperature_effective": 1, "lead_temperature": 1,
         "created_at": 1, "conversation_status": 1, "conversation_status_reason": 1,
         "price_drop_v2": 1},
    )
    if hasattr(docs, "batch_size"):
        docs = docs.batch_size(500)
    for lead in docs:
        lead_id = str(lead.get("_id") or "")
        lead_temp = str(lead.get("lead_temperature_effective") or
                        lead.get("lead_temperature") or "").strip().upper()
        if not temperature_filter_matches(lead_temp, selected_temperature):
            continue
        age = lead_age_details(lead, now=stamp, max_age_days=age_limit)
        if age["lead_age_status"] != "valid" or age["excluded_by_age"]:
            continue
        reply_events = (lead.get("price_drop_v2") or {}).get("reply_events") or []
        for campaign in ((lead.get("price_drop_v2") or {}).get("campaigns") or []):
            if campaign.get("status") not in status_set:
                continue
            cid = str(campaign.get("event_id") or campaign.get("campaign_id") or "")
            if not cid:
                continue
            campaign_at = _campaign_stamp(campaign)
            if since and (campaign_at is None or campaign_at < since):
                continue
            code = str(campaign.get("property_code") or "")
            key = (cid, lead_id)
            event = by_event.setdefault(cid, {
                "event_id": cid, "property_code": code,
                "currency": str(campaign.get("currency") or "UF"),
                "old_price": campaign.get("old_price_clp") if campaign.get("currency") == "CLP"
                             else campaign.get("old_price_uf"),
                "new_price": campaign.get("new_price_clp") if campaign.get("currency") == "CLP"
                             else campaign.get("new_price_uf"),
                "accepted": set(), "delivered": set(), "read": set(), "pending": set(),
                "interested": set(), "last_recorded_at": None,
            })
            status = str(campaign.get("status") or "").upper()
            provider_status = str(campaign.get("provider_status") or "").lower()
            if status in {"SENT", "RESPONDED", "DELIVERED", "READ", "OPTED_OUT", "DECLINED"} or campaign.get("provider_message_id"):
                event["accepted"].add(lead_id)
                global_sets["accepted"].add(key)
            if provider_status in {"delivered", "read", "seen"} or status == "DELIVERED":
                event["delivered"].add(lead_id)
                global_sets["delivered"].add(key)
            if provider_status in {"read", "seen"} or status == "READ":
                event["read"].add(lead_id)
                global_sets["read"].add(key)
            if status in {"PREPARED", "APPROVED", "SENDING", "DELIVERY_UNKNOWN"}:
                event["pending"].add(lead_id)
                global_sets["pending"].add(key)
            if status == "PREPARED":
                global_sets["prepared"].add(key)
            if status == "APPROVED":
                global_sets["approved"].add(key)
            if status in {"SENDING", "DELIVERY_UNKNOWN"}:
                global_sets["uncertain"].add(key)
            if status == "FAILED":
                global_sets["failed"].add(key)
            if int(campaign.get("reply_count") or 0) > 0:
                global_sets["responses"].add(key)
            classifications = [
                row.get("classification") or {}
                for row in reply_events
                if str(row.get("campaign_id") or "") == str(campaign.get("campaign_id"))
                and isinstance(row, dict)
            ]
            classifications.extend(row for row in (
                campaign.get("last_classification"),
                campaign.get("last_positive_classification"),
            ) if isinstance(row, dict))
            if campaign.get("has_positive_response") is True:
                event["interested"].add(lead_id)
                global_sets["interested"].add(key)
            for classification in classifications:
                label = str(classification.get("label") or "")
                if classification.get("visit_requested") or classification.get("intent") == "ASK_VISIT":
                    global_sets["visits"].add(key)
                if classification.get("offer_intent") or classification.get("intent") == "GIVE_OFFER":
                    global_sets["offers"].add(key)
                if classification.get("human_requested") or classification.get("intent") in {"ASK_CONTACT", "CALL"}:
                    global_sets["calls"].add(key)
                if classification.get("alternatives_requested") or label == "BUSCA_ALTERNATIVAS":
                    global_sets["alternatives"].add(key)
                if label == "BAJA" or classification.get("reason") == "explicit_opt_out":
                    global_sets["optouts"].add(key)
                if classification.get("reason") == "already_found_property":
                    global_sets["search_complete"].add(key)
            if (lead.get("conversation_status") == "STOPPED_BY_CLIENT"
                    and str(lead.get("conversation_status_reason") or "") == "already_found_property"):
                global_sets["search_complete"].add(key)
            if campaign_at and (event["last_recorded_at"] is None or campaign_at > event["last_recorded_at"]):
                event["last_recorded_at"] = campaign_at
            if campaign_at and (latest_stamp is None or campaign_at > latest_stamp):
                latest_stamp = campaign_at

    # Active price drops with no campaign yet still appear as opportunities.
    for code, current in current_by_code.items():
        by_event.setdefault(current.event_id, {
            "event_id": current.event_id, "property_code": code,
            "currency": current.currency, "old_price": current.old_price,
            "new_price": current.new_price, "accepted": set(), "delivered": set(),
            "read": set(), "pending": set(), "interested": set(), "last_recorded_at": None,
        })

    codes = sorted(set(current_by_code) | {row["property_code"] for row in by_event.values()})
    print("Código | Evento | Rebaja vigente / último registro | Elegibles | Aceptados WaSender | Pendientes | Interesados")
    property_metrics: dict[str, dict[str, set[tuple[str, str]]]] = {}
    for cid, row in by_event.items():
        metrics = property_metrics.setdefault(row["property_code"], {
            "accepted": set(), "pending": set(), "interested": set(),
        })
        for key in ("accepted", "pending", "interested"):
            metrics[key].update((cid, lead_id) for lead_id in row[key])
    for code in codes:
        active = current_by_code.get(code)
        history_rows = [(cid, row) for cid, row in by_event.items()
                        if row["property_code"] == code]
        selected = next(((cid, row) for cid, row in history_rows
                         if active and cid == active.event_id), None)
        if selected is None and history_rows:
            selected = max(history_rows, key=lambda item: (
                item[1]["last_recorded_at"] or datetime.min.replace(tzinfo=timezone.utc), item[0]
            ))
        if selected is None:
            continue
        cid, row = selected
        is_current = bool(active and active.event_id == cid)
        prices = ((active.old_price, active.new_price, active.currency) if is_current else
                  (row["old_price"], row["new_price"], row["currency"]))
        label = (f"{format_price(prices[0], prices[2])} → {format_price(prices[1], prices[2])}"
                 if prices[0] is not None and prices[1] is not None else "N/D")
        metrics = property_metrics.get(code, {})
        print(f"{code} | {cid} | {label}{' (vigente)' if is_current else ' (histórica)'} | "
              f"{candidate_counts.get(cid, 0)} | {len(metrics.get('accepted', set()))} | "
              f"{len(metrics.get('pending', set()))} | {len(metrics.get('interested', set()))}")

    print("\nTotales generales:")
    print(f"Propiedades con rebajas vigentes: {len(current_by_code)}")
    print(f"Leads elegibles en eventos vigentes: {candidate_total}")
    print(f"Leads excluidos solo por antigüedad: {age_excluded}")
    print(f"Mensajes aceptados por WaSender: {len(global_sets['accepted'])}")
    print(f"Fallidos confirmados: {len(global_sets.get('failed', set()))}")
    print(f"Entregados confirmados: {len(global_sets['delivered'])} | leídos confirmados: {len(global_sets['read'])}")
    print(f"Respuestas recibidas: {len(global_sets['responses'])} | interés: {len(global_sets['interested'])}")
    print(f"Visitas: {len(global_sets['visits'])} | ofertas: {len(global_sets['offers'])} | llamadas: {len(global_sets['calls'])}")
    print(f"Alternativas: {len(global_sets['alternatives'])} | bajas: {len(global_sets['optouts'])} | "
          f"búsqueda terminada: {len(global_sets['search_complete'])}")
    print(f"Envíos inciertos: {len(global_sets['uncertain'])} | pendientes de aprobación: {len(global_sets['prepared'])} | "
          f"aprobados pendientes de envío: {len(global_sets['approved'])}")
    print(f"Último intento/envío registrado: {latest_stamp.astimezone(CHILE_TZ).isoformat() if latest_stamp else 'N/A'}")
    return {
        "status": "COMPLETED", "properties_with_current_drops": len(current_by_code),
        "eligible_current": candidate_total, "excluded_age_only": age_excluded,
        "accepted": len(global_sets["accepted"]), "delivered": len(global_sets["delivered"]),
        "read": len(global_sets["read"]), "responses": len(global_sets["responses"]),
        "interested": len(global_sets["interested"]),
        "visits": len(global_sets["visits"]), "offers": len(global_sets["offers"]),
        "calls": len(global_sets["calls"]), "alternatives": len(global_sets["alternatives"]),
        "optouts": len(global_sets["optouts"]),
        "search_complete": len(global_sets["search_complete"]),
        "uncertain": len(global_sets["uncertain"]),
        "failed": len(global_sets["failed"]),
        "pending_approval": len(global_sets["prepared"]),
        "approved_pending_send": len(global_sets["approved"]),
        "last_activity_at": latest_stamp,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Descubre rebajas Prop360 y opera campañas V2 aprobadas de forma manual."
    )
    actions = parser.add_mutually_exclusive_group()
    for flag in ("discover", "dry-run", "audience-audit", "provider-audit", "interactive", "execute", "resume", "prepare", "approve",
                 "review-whatsapp-contact", "report", "summary"):
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
    parser.add_argument("--temperature", type=_parse_temperature_arg,
                        help="HOT, NORMAL, COLD, HOT,NORMAL o TODOS; desconocidos requieren revisión manual")
    parser.add_argument("--lead-max-age-days", type=_parse_lead_age_arg,
                        help="edad por leads.created_at; 30/90/120/180; 0 significa sin límite expresamente")
    parser.add_argument("--period-days", type=int,
                        help="para --summary, limitar campañas históricas a los últimos N días")
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
    if args.pilot and (args.temperature is not None or args.lead_max_age_days is not None):
        raise CliError("new_filters_are_separate_from_the_approved_three_recipient_pilot")
    if args.period_days is not None:
        if args.period_days < 1 or args.action != "summary":
            raise CliError("period_days_requires_summary_and_must_be_positive")
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
    if args.action == "review-whatsapp-contact" and len(args.lead_id) != 1:
        raise CliError("whatsapp_authorization_review_requires_one_lead_id")
    if args.action == "prepare" and not args.contact_basis_reviewed:
        raise CliError("contact_basis_review_required")
    if (args.action in {"prepare", "approve", "review-whatsapp-contact"}
            and not args.lead_id and not (args.pilot and args.action == "review-whatsapp-contact")):
        raise CliError("lead_id_required")
    if args.action == "execute" and not args.pilot and not args.campaign_id:
        raise CliError("campaign_id_required_for_execution")
    if args.campaign_id == PILOT_CAMPAIGN_ID and not args.pilot:
        raise CliError("approved_three_recipient_campaign_requires_pilot_selector")
    return since


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

        if args.status:
            if args.pilot:
                print(json.dumps(_pilot_status(db), ensure_ascii=False, default=str))
                return 0
            return _report(db, args.campaign_id)
        if args.action == "report":
            return _report(db, args.campaign_id)
        if args.action == "summary":
            _summary_report(
                db, since=since, period_days=args.period_days,
                temperature_filter=args.temperature or "ALL",
                lead_max_age_days=(args.lead_max_age_days if args.lead_max_age_days is not None
                                   else DEFAULT_LEAD_MAX_AGE_DAYS),
            )
            return 0
        if args.action == "provider-audit":
            # GET-only account verification. Never send, prepare or approve.
            usage = _provider_attempts_last_24h(now=datetime.now(timezone.utc))
            print(json.dumps({
                "status": "READY", "provider": "WaSender",
                "send_attempts": 0,
                "account_usage": usage,
            }, ensure_ascii=False, default=str))
            return 0
        if args.action == "audience-audit":
            report = audit_full_portfolio(
                db, since=since, property_code=args.property_code,
                currency=args.currency, operation=args.operation,
                temperature_filter=args.temperature or "ALL",
                lead_max_age_days=(args.lead_max_age_days if args.lead_max_age_days is not None
                                   else DEFAULT_LEAD_MAX_AGE_DAYS),
                limit=args.limit, batch_size=args.batch_size,
            )
            print(json.dumps(report, ensure_ascii=False, default=str))
            return 0
        if args.action == "discover":
            return _print_events(db, detailed=False, since=since,
                                 property_code=args.property_code,
                                 currency=args.currency, operation=args.operation,
                                 limit=args.limit, batch_size=args.batch_size,
                                 temperature_filter=args.temperature or "ALL",
                                 lead_max_age_days=(args.lead_max_age_days if args.lead_max_age_days is not None
                                                    else DEFAULT_LEAD_MAX_AGE_DAYS))
        if args.action == "dry-run":
            return _print_events(db, detailed=True, since=since,
                                 property_code=args.property_code,
                                 currency=args.currency, operation=args.operation,
                                 limit=args.limit, batch_size=args.batch_size,
                                 temperature_filter=args.temperature or "ALL",
                                 lead_max_age_days=(args.lead_max_age_days if args.lead_max_age_days is not None
                                                    else DEFAULT_LEAD_MAX_AGE_DAYS))
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
                temperature_filter=args.temperature,
                lead_max_age_days=args.lead_max_age_days,
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
                temperature_filter=args.temperature or "ALL",
                lead_max_age_days=(args.lead_max_age_days if args.lead_max_age_days is not None
                                   else DEFAULT_LEAD_MAX_AGE_DAYS),
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
            review_campaign_id = PILOT_CAMPAIGN_ID if args.pilot else args.campaign_id
            expected = _confirm_token("REVISAR_WHATSAPP", review_campaign_id)
            if args.confirm_action != expected:
                raise CliError(f"type_exact_confirm_action:{expected}")
            lead_ids = args.lead_id
            if args.pilot:
                pilot_rows = _entries(db, PILOT_CAMPAIGN_ID, ("PREPARED", "APPROVED"))
                codes = {str(row["campaign"].get("property_code") or "") for row in pilot_rows}
                if len(pilot_rows) != 3 or codes != PILOT_PROPERTY_CODES:
                    raise CliError("pilot_whatsapp_review_requires_exact_three_unsent_rows")
                pilot_ids = {str(row["lead"].get("_id")) for row in pilot_rows}
                if len(lead_ids) != 1 or str(lead_ids[0]) not in pilot_ids:
                    raise CliError("pilot_whatsapp_review_requires_one_persisted_lead_id")
                lead_ids = [str(lead_ids[0])]
            ids = [_lead_id(value) for value in dict.fromkeys(lead_ids)]
            backup = _backup_leads(db, ids)
            result = review_selected_whatsapp_contact(
                db, campaign_id=review_campaign_id, lead_ids=lead_ids,
                operator=args.operator, evidence=args.whatsapp_contact_evidence,
            )
            print(json.dumps({**result, "backup": str(backup)}, ensure_ascii=False, default=str))
            return 0
        if args.action in {"execute", "resume"}:
            if args.pilot:
                pilot_rows = _validated_pilot_rows(db)
                pilot_statuses = {row["campaign"].get("status") for row in pilot_rows}
                if args.action == "execute" and pilot_statuses != {"APPROVED"}:
                    raise CliError("pilot_initial_execution_requires_three_approved_unsent_rows_use_resume")
                if args.action == "resume" and any(
                        status not in {"APPROVED", "SENT", "RESPONDED", "DECLINED", "OPTED_OUT"}
                        for status in pilot_statuses):
                    raise CliError("pilot_has_uncertain_or_unreviewed_state_reconcile_first")
                campaigns = {
                    PILOT_CAMPAIGN_ID: {
                        str(row["lead"]["_id"]) for row in pilot_rows
                        if row["campaign"].get("status") == "APPROVED"
                    }
                }
                if args.lead_id:
                    raise CliError("pilot_recipient_set_is_fixed_by_approved_campaign_rows")
            else:
                campaigns = _approved_campaign_batch(
                    db, campaign_id=args.campaign_id,
                )
                if args.lead_id:
                    wanted = set(args.lead_id)
                    if args.campaign_id:
                        campaigns[args.campaign_id] = campaigns.get(args.campaign_id, set()) & wanted
                    else:
                        campaigns = {cid: ids & wanted for cid, ids in campaigns.items()}
                        campaigns = {cid: ids for cid, ids in campaigns.items() if ids}
            if not any(campaigns.values()):
                print(json.dumps({"status": "NO_APPROVED_RECIPIENTS", "attempts": 0},
                                 ensure_ascii=False))
                return 0
            result = execute_approved_batch(
                db, campaigns=campaigns, include_pilot=args.pilot,
                max_messages=args.max_messages,
                min_delay_seconds=args.min_delay_seconds,
                max_delay_seconds=args.max_delay_seconds,
                start_time=args.start_time, end_time=args.end_time,
                allowed_weekdays=args.allowed_weekdays,
                timezone_name=args.timezone, provider_plan=args.provider_plan,
                daily_provider_cap=args.daily_provider_cap,
                temperature_filter=args.temperature,
                lead_max_age_days=args.lead_max_age_days,
                enforce_campaign_filters=not args.pilot,
                confirm=input,
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
