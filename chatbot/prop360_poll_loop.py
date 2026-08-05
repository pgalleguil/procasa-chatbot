"""Prop360 poll loop — periodic ingestion of recent Convecta leads.

Runs every PROP360_POLL_INTERVAL_SECONDS. Each cycle:
  1. Logs into Prop360 (fresh session per cycle).
  2. Fetches leads from the last PROP360_POLL_WINDOW_HOURS (default 24h).
  3. Skips internal executives and properties not in the active cartera.
  4. Delegates identity/dedup to ingest_lead_event (phone/email lookup on the
     `leads` collection), so WhatsApp/manual/Prop360 converge on one document.
  5. Enqueues HOT or non-HOT digest notifications via the canonical flow.

It is fully isolated from the chatbot, SLA and digest loops: an exception in
one cycle never stops the other workers.
"""
from __future__ import annotations

import asyncio
import logging
import os
import traceback
from datetime import datetime, timedelta

logger = logging.getLogger("prop360.poll")


def _feature_enabled() -> bool:
    return os.getenv("PROP360_POLL_ENABLED", "false").lower() == "true"


def _interval_seconds() -> int:
    return int(os.getenv("PROP360_POLL_INTERVAL_SECONDS", "3600"))


def _window_hours() -> int:
    return int(os.getenv("PROP360_POLL_WINDOW_HOURS", "24"))


def _credentials() -> tuple:
    email = os.getenv("PROP360_EMAIL")
    password = os.getenv("PROP360_PASSWORD")
    return email, password


def _internal_domains() -> list:
    return ["procasa.cl"]


def _active_cartera_codes() -> set:
    from .storage import get_db
    db = get_db()
    codes = set()
    for d in db["universo_cartera_prop360"].find(
        {"disponible_prop360": True}, {"codigo": 1}
    ):
        code = str(d.get("codigo") or "").strip()
        if code:
            codes.add(code)
    return codes


def _from_date_iso(window_hours: int) -> str:
    start = datetime.now() - timedelta(hours=window_hours)
    return start.strftime("%Y-%m-%dT00:00:00")


def _is_internal(norm: dict) -> bool:
    email = str(norm.get("contMail") or "").strip().lower()
    return any(email.endswith(f"@{d}") for d in _internal_domains())


def _norm_has_contact(norm: dict) -> bool:
    f = str(norm.get("contFono") or "").strip()
    em = str(norm.get("contMail") or "").strip().lower()
    return bool(f or em)


def run_prop360_poll_cycle(db=None) -> dict:
    """One full poll cycle. Returns metrics dict; never raises."""
    if not _feature_enabled():
        return {"status": "disabled", "fetched": 0, "created": 0, "updated": 0}

    email, password = _credentials()
    if not email or not password:
        logger.error("[PROP360_POLL] missing PROP360_EMAIL/PROP360_PASSWORD env")
        return {"status": "error", "reason": "missing_credentials"}

    from scraping_convecta.extractor_prop360 import Prop360Extractor

    if db is None:
        from .storage import get_db
        db = get_db()

    window_hours = _window_hours()
    extractor = Prop360Extractor(email=email, password=password, dry_run=False)

    metrics = {
        "status": "ok",
        "started_at": datetime.now().isoformat(),
        "fetched": 0,
        "leads_created": 0,
        "leads_updated": 0,
        "duplicates_skipped": 0,
        "events_duplicate": 0,
        "notifications_enqueued": 0,
        "errors": 0,
        "skipped_internal": 0,
        "skipped_no_contact": 0,
        "skipped_property_inactive": 0,
        "skipped_stale_id": 0,
    }

    try:
        extractor.login()
    except Exception:
        logger.error("[PROP360_POLL] login failed:\n%s", traceback.format_exc())
        metrics["status"] = "error"
        metrics["reason"] = "login_failed"
        _save_checkpoint(extractor, metrics, db)
        return metrics

    try:
        from_date = _from_date_iso(window_hours)
        leads = extractor.fetch_leads(from_date=from_date, to_date=None)
    except Exception:
        logger.error("[PROP360_POLL] fetch failed:\n%s", traceback.format_exc())
        metrics["status"] = "error"
        metrics["reason"] = "fetch_failed"
        _save_checkpoint(extractor, metrics, db)
        return metrics

    metrics["fetched"] = len(leads)
    cartera = _active_cartera_codes()
    last_id = _last_contacto_id(db)
    max_confirmed_id = last_id or 0

    for raw in leads:
        try:
            norm = extractor.normalize_lead(raw)
        except Exception:
            metrics["errors"] += 1
            continue

        cid = _norm_id(norm)

        # Monotonic idContacto watermark: leads already seen before are skipped
        # without any DB lookup. Falls back to the identity dedup below when the
        # checkpoint is missing or the id is unknown.
        if cid is not None and last_id is not None and cid <= last_id:
            metrics["skipped_stale_id"] += 1
            if cid > max_confirmed_id:
                max_confirmed_id = cid
            continue

        if _is_internal(norm):
            metrics["skipped_internal"] += 1
            continue

        if not _norm_has_contact(norm):
            metrics["skipped_no_contact"] += 1
            continue

        prop = str(norm.get("codigo") or "").strip()
        if prop and prop not in cartera:
            metrics["skipped_property_inactive"] += 1
            continue

        # _is_duplicate re-checks identity on the leads collection before ingest
        if extractor._is_duplicate(norm):
            metrics["duplicates_skipped"] += 1
            if cid and cid > max_confirmed_id:
                max_confirmed_id = cid
            continue

        try:
            extractor.process_lead(raw)
        except Exception:
            logger.error(
                "[PROP360_POLL] process_lead failed idContacto=%s:\n%s",
                norm.get("idContacto"), traceback.format_exc(),
            )
            metrics["errors"] += 1
            continue

        m = extractor.metrics
        metrics["leads_created"] = m["leads_created"]
        metrics["leads_updated"] = m["leads_updated"]
        metrics["events_duplicate"] = m["events_duplicate"]
        metrics["notifications_enqueued"] = m["notifications_enqueued"]
        if cid and cid > max_confirmed_id:
            max_confirmed_id = cid

    metrics["finished_at"] = datetime.now().isoformat()
    _save_checkpoint(extractor, metrics, db, max_confirmed_id=max_confirmed_id)
    logger.info(
        "[PROP360_POLL] cycle done: fetched=%s created=%s updated=%s "
        "dupes=%s stale_id=%s notif=%s errors=%s",
        metrics["fetched"], metrics["leads_created"], metrics["leads_updated"],
        metrics["duplicates_skipped"], metrics["skipped_stale_id"],
        metrics["notifications_enqueued"], metrics["errors"],
    )
    return metrics


def _norm_id(norm: dict):
    cid = norm.get("idContacto")
    try:
        return int(cid) if cid is not None and str(cid).strip() else None
    except (TypeError, ValueError):
        return None


def _last_contacto_id(db) -> int | None:
    """Highest idContacto seen so far (persisted checkpoint, else None)."""
    try:
        from scraping_convecta.extractor_prop360 import get_last_extraction_state
        state = get_last_extraction_state()
        cid = state.get("last_id_contacto")
        return int(cid) if cid is not None else None
    except Exception:
        return None


def _save_checkpoint(extractor, metrics: dict, db, max_confirmed_id: int | None = None) -> None:
    """Persist run metrics + monotonic id watermark (heartbeat for monitoring)."""
    try:
        from scraping_convecta.extractor_prop360 import save_extraction_state
        if max_confirmed_id is not None:
            extractor.metrics["last_id_contacto"] = max_confirmed_id
        extractor.metrics.update(metrics)
        save_extraction_state(extractor.metrics)
    except Exception:
        logger.error("[PROP360_POLL] checkpoint save failed:\n%s", traceback.format_exc())


async def prop360_poll_loop(sleep_seconds: int | None = None) -> None:
    """Main loop for Prop360 ingestion. Feature-flagged and self-contained."""
    if not _feature_enabled():
        logger.info("[PROP360_POLL] Disabled (PROP360_POLL_ENABLED != true). Loop exit.")
        return

    interval = sleep_seconds or _interval_seconds()
    logger.info("[PROP360_POLL] Loop started. interval=%ss window_hours=%s",
                interval, _window_hours())

    while True:
        try:
            await asyncio.to_thread(run_prop360_poll_cycle)
        except Exception:
            logger.error("[PROP360_POLL] Loop cycle error:\n%s", traceback.format_exc())
        await asyncio.sleep(interval)
