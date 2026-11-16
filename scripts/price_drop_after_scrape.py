"""Connect a successful local Prop360 refresh to verified price-drop campaigns.

Only executed after the scraper's explicit --execute has committed MongoDB
updates.  The office-only sender is opt-in and fails closed if any runtime
gate, source event, business-hours or provider validation is unavailable.
"""
from __future__ import annotations

import os
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from chatbot.price_drop_campaign_discovery import HISTORY_COLLECTION
from chatbot.price_drop_leads_v2 import CONTEXT_FLAG, SEND_FLAG


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


def _during_business_hours(now: datetime) -> bool:
    stamp = now.astimezone(ZoneInfo("America/Santiago"))
    return stamp.weekday() < 5 and time(9) <= stamp.time().replace(tzinfo=None) < time(19)


def verified_scrape_change_keys(db, *, sync_run_id: str) -> set[tuple]:
    """Only canonical verified reductions newly committed by THIS scrape.

    The manual scraper writes these events atomically with its property
    updates.  We do not infer sends from old historical/embedded records,
    currency conversions, price rises, or other offices.
    """
    if not isinstance(sync_run_id, str) or not sync_run_id.startswith("manual_scrape_"):
        return set()
    rows = db[HISTORY_COLLECTION].find({
        "sync_run_id": sync_run_id,
        "campo": "precio_publicado",
        "tipo_evento": "rebaja_confirmada",
        "operacion": {"$in": ["venta", "arriendo"]},
        "moneda_anterior": {"$in": ["UF", "CLP"]},
    }, {
        "_id": 0, "codigo": 1, "operacion": 1,
        "moneda_anterior": 1, "moneda_nueva": 1,
        "monto_anterior": 1, "monto_nuevo": 1,
    })
    keys = set()
    for row in rows:
        currency = row.get("moneda_anterior")
        if currency != row.get("moneda_nueva"):
            continue
        try:
            old, new = float(row["monto_anterior"]), float(row["monto_nuevo"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not 0 < new < old:
            continue
        keys.add((str(row.get("codigo") or ""), row["operacion"], currency, old, new))
    return keys


def dispatch_after_scrape(*, sync_run_id: str, started_at: datetime, db=None,
                          now: datetime | None = None) -> dict:
    """Trigger one bounded, paced office-phone batch after a successful scrape.

    This call does NOT schedule future work. Outside the business-hours window
    it returns a clear blocked status without preparing a campaign.
    """
    result = {"source": "manual_prop360_scrape", "status": "DISABLED", "attempts": 0,
              "sync_run_id": str(sync_run_id or "")}
    if not _enabled("PRICE_DROP_AFTER_SCRAPE_ENABLED"):
        return result
    if _enabled("RENDER") or os.getenv("RENDER_SERVICE_ID"):
        return {**result, "status": "BLOCKED_RENDER_RUNTIME"}
    if not (_enabled(SEND_FLAG) and _enabled(CONTEXT_FLAG)):
        return {**result, "status": "BLOCKED_CAMPAIGN_RUNTIME_GATES"}
    now = now or datetime.now(timezone.utc)
    if not _during_business_hours(now):
        return {**result, "status": "BLOCKED_OUTSIDE_BUSINESS_HOURS"}

    if db is None:
        # Same local MongoDB configuration as the existing CLI. No added
        # credentials, cloud scheduler or additional WhatsApp session.
        from scripts.enviar_piloto_rebajas import _get_db
        db = _get_db()
    keys = verified_scrape_change_keys(db, sync_run_id=sync_run_id)
    if not keys:
        return {**result, "status": "NO_NEW_VERIFIED_PRICE_DROPS"}

    from scripts.enviar_rebajas_automatico import run as campaign_run
    outcome = campaign_run(
        db, execute=True, max_messages=5, max_age_days=90,
        min_delay=300, max_delay=600, daily_provider_cap=50,
        provider_plan="trial", now=now, since=started_at,
        allowed_change_keys=keys,
    )
    return {**result, "status": outcome.get("status", "UNKNOWN"),
            "matched_new_drops": len(keys),
            "selected": outcome.get("selected", 0),
            "attempts": outcome.get("attempts", 0),
            "campaign_result": outcome}
