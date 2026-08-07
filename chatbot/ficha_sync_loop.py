"""Ficha sync loop — monthly sync of property fichas.

Runs on the same in-process pattern as ``prop360_poll_loop``: an asyncio loop
launched from ``webhook.py``, feature-flagged via env vars.

Schedule (Chile time):
    - Monthly backfill on Sunday within ``FICHA_SYNC_BACKFILL_HOUR`` +
      ``FICHA_SYNC_BACKFILL_WINDOW``, at most once every
      ``FICHA_SYNC_BACKFILL_MIN_DAYS`` (default 30 -> ~1x per month,
      early morning, end or start of month).
    - No incremental runs: re-scraping every cycle wastes bandwidth on the
      Hobby plan, so the full cartera is refreshed once a month.

Env vars:
    FICHA_SYNC_ENABLED              "true"/"false" (default "false")
    FICHA_SYNC_BACKFILL_HOUR        start hour (Chile time) for backfill (default 4)
    FICHA_SYNC_BACKFILL_WINDOW      hours window after start hour (default 3)
    FICHA_SYNC_BACKFILL_MIN_DAYS    min days between backfills (default 30)

It is fully isolated: an exception in one cycle never stops the other workers.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import traceback
from datetime import datetime, timedelta
from types import SimpleNamespace

logger = logging.getLogger("ficha.sync")

try:
    from chatbot.constants import CHILE_TZ
except Exception:  # pragma: no cover
    CHILE_TZ = None

# No incremental slots: only the monthly Sunday backfill runs.
INCREMENTAL_SLOTS = ()


def _feature_enabled() -> bool:
    return os.getenv("FICHA_SYNC_ENABLED", "false").lower() == "true"


def _backfill_hour() -> int:
    return int(os.getenv("FICHA_SYNC_BACKFILL_HOUR", "4"))


def _backfill_window_hours() -> int:
    return int(os.getenv("FICHA_SYNC_BACKFILL_WINDOW", "3"))


def _backfill_min_days() -> int:
    return int(os.getenv("FICHA_SYNC_BACKFILL_MIN_DAYS", "30"))


def _now_local() -> datetime:
    if CHILE_TZ is not None:
        return datetime.now(CHILE_TZ)
    return datetime.now()


def _runtime_diag(enabled: bool | None = None) -> dict:
    if enabled is None:
        enabled = _feature_enabled()
    return {
        "enabled": enabled,
        "incremental_slots": list(INCREMENTAL_SLOTS),
        "backfill_hour": _backfill_hour(),
        "backfill_window_hours": _backfill_window_hours(),
        "backfill_min_days": _backfill_min_days(),
        "has_email": bool(os.getenv("PROP360_EMAIL")),
        "has_password": bool(os.getenv("PROP360_PASSWORD")),
    }


def _update_health_heartbeat(**extra) -> None:
    try:
        import webhook as _wh
        st = getattr(_wh, "background_tasks_status", None)
        if st is None:
            return
        st.setdefault("ficha_sync", {"status": "starting", "last_heartbeat": None})
        st["ficha_sync"]["last_heartbeat"] = _now_local().isoformat()
        st["ficha_sync"].update(extra)
    except Exception:
        pass


def _persist_cycle_status(status: str, extra: dict | None = None) -> None:
    """Persist a heartbeat/doc for observability in all exit paths."""
    try:
        from .storage import get_db
        db = get_db()
        doc = {
            "updated_at": datetime.utcnow().isoformat(),
            "status": status,
            **_runtime_diag(),
        }
        if extra:
            doc.update(extra)
        db["ficha_sync_status"].update_one(
            {"_id": "last"}, {"$set": doc}, upsert=True
        )
    except Exception:
        logger.error("[FICHA_SYNC] status persist failed:\n%s", traceback.format_exc())


def _slot_hours_for_weekday(weekday: int) -> list[int]:
    """Chile-schedule hours for a weekday. Python: 0=Mon ... 6=Sun."""
    if weekday == 5:      # Saturday
        return []
    if weekday == 6:      # Sunday
        return [_backfill_hour()]
    return list(INCREMENTAL_SLOTS)  # Mon-Fri


def _next_run_at(now_local: datetime) -> datetime:
    """Next scheduled run time strictly after ``now_local`` (Chile time)."""
    for day_offset in range(8):
        day = now_local + timedelta(days=day_offset)
        for hour in sorted(_slot_hours_for_weekday(day.weekday())):
            candidate = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate > now_local:
                return candidate
    return now_local + timedelta(days=1)


def _current_mode(now_local: datetime) -> str | None:
    """Return 'incremental', 'backfill' or None when no run is due now."""
    weekday = now_local.weekday()
    hour = now_local.hour
    if weekday == 6:  # Sunday
        start = _backfill_hour()
        if start <= hour < start + _backfill_window_hours():
            return "backfill"
        return None
    if weekday == 5:  # Saturday
        return None
    if hour in INCREMENTAL_SLOTS:
        return "incremental"
    return None


def _backfill_due(db) -> bool:
    """True when enough days passed since the last successful backfill."""
    try:
        doc = db["ficha_sync_status"].find_one({"_id": "last"}) or {}
        last_raw = doc.get("last_backfill_at")
        if not last_raw:
            return True
        last_dt = datetime.fromisoformat(str(last_raw))
        return (datetime.utcnow() - last_dt) >= timedelta(days=_backfill_min_days())
    except Exception:
        return True


def _build_args(backfill: bool) -> argparse.Namespace:
    return SimpleNamespace(
        dry_run=False,
        codigo=None,
        max_new=None,
        max_update=None,
        limit=None,
        backfill=backfill,
        no_bajas=False,
        delay=0.1,
    )


def run_ficha_sync_cycle(db=None) -> dict:
    """One full sync cycle. Returns metrics dict; never raises."""
    metrics = {
        "status": "idle",
        "started_at": datetime.utcnow().isoformat(),
        "mode": None,
        "exit_code": None,
    }
    if not _feature_enabled():
        _persist_cycle_status("disabled", {"reason": "flag_off"})
        return {"status": "disabled", "mode": None}

    email = os.getenv("PROP360_EMAIL")
    password = os.getenv("PROP360_PASSWORD")
    if not email or not password:
        logger.error("[FICHA_SYNC] missing PROP360_EMAIL/PROP360_PASSWORD env")
        _persist_cycle_status("error", {"reason": "missing_credentials"})
        return {"status": "error", "reason": "missing_credentials", "mode": None}

    mode = _current_mode(_now_local())
    if mode is None:
        _persist_cycle_status("idle", {"mode": None, "reason": "outside_schedule"})
        return metrics

    if db is None:
        from .storage import get_db
        db = get_db()

    backfill = False
    if mode == "backfill":
        if not _backfill_due(db):
            logger.info("[FICHA_SYNC] backfill not due yet; skipping.")
            _persist_cycle_status("skipped_backfill", {"mode": "backfill"})
            return {"status": "skipped_backfill", "mode": "backfill"}
        backfill = True

    metrics["mode"] = "backfill" if backfill else "incremental"
    try:
        from scraping_convecta.scraping_prop360_ficha_completa import run
        args = _build_args(backfill)
        exit_code = run(args)
        metrics["exit_code"] = exit_code
        metrics["status"] = "error" if exit_code else "ok"
        metrics["reason"] = "exit_code_nonzero" if exit_code else None
    except Exception:
        metrics["status"] = "error"
        metrics["reason"] = "exception"
        logger.error("[FICHA_SYNC] cycle error:\n%s", traceback.format_exc())
        _persist_cycle_status("error", {"reason": "exception", "mode": metrics["mode"]})
        return metrics

    extra = {"mode": metrics["mode"], "exit_code": exit_code}
    if backfill:
        extra["last_backfill_at"] = datetime.utcnow().isoformat()
    _persist_cycle_status(metrics["status"], extra)
    logger.info(
        "[FICHA_SYNC] cycle done: mode=%s exit_code=%s",
        metrics["mode"], exit_code,
    )
    return metrics


async def ficha_sync_loop(sleep_seconds: int | None = None) -> None:
    """Main loop for the ficha sync. Feature-flagged and self-contained."""
    if not _feature_enabled():
        _update_health_heartbeat(status="disabled")
        logger.info("[FICHA_SYNC] Disabled (FICHA_SYNC_ENABLED != true). Loop exit.")
        return

    _update_health_heartbeat(status="running", **{})
    logger.info(
        "[FICHA_SYNC] Loop started. slots=%s backfill_hour=%s window=%sh min_days=%s",
        INCREMENTAL_SLOTS, _backfill_hour(), _backfill_window_hours(),
        _backfill_min_days(),
    )

    first = True
    while True:
        if not first:
            await _sleep_until_next_slot()
        first = False
        try:
            result = await asyncio.to_thread(run_ficha_sync_cycle)
            _update_health_heartbeat(status="running", last_cycle=result.get("status"))
        except Exception:
            _update_health_heartbeat(status="error")
            tb = traceback.format_exc()
            _persist_cycle_status("error", {"traceback": tb[-2000:]})
            logger.error("[FICHA_SYNC] Loop cycle error:\n%s", tb)
        await _sleep_until_next_slot()


async def _sleep_until_next_slot() -> None:
    """Sleep until the next scheduled run (Chile time), guarded against drift."""
    now_local = _now_local()
    next_slot = _next_run_at(now_local)
    wait = max((next_slot - now_local).total_seconds(), 5)
    logger.info("[FICHA_SYNC] next cycle at %s (%s)",
                next_slot.isoformat(), f"in {int(wait)}s")
    await asyncio.sleep(wait)
