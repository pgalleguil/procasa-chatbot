"""uf_sync_loop.py — Actualización diaria de la UF y de los precios derivados.

Mismo patrón que ficha_sync_loop/prop360_poll_loop: loop asyncio in-process
lanzado desde webhook.py lifespan, feature-flagged por env vars.

Frecuencia inicial: DIARIA (UF_SYNC_HOUR, default 04:00 Chile).

Cada ciclo (con flag habilitado):
  A. Obtiene UNA vez la UF vigente desde mindicador.cl.
  B. Si éxito: persiste en `uf_cache` y actualiza derivados de propiedades
     activas (moneda_publicada=CLP -> recalcula solo precio_uf; UF -> solo
     precio_clp). JAMÁS toca el precio original.
  C. Si Mindicador falla: conserva la UF anterior, no modifica precios,
     warning, y reintenta en la próxima ejecución.

Env vars:
    UF_SYNC_ENABLED   "true"/"false" (default "false")
    UF_SYNC_HOUR      hora Chile para la corrida diaria (default 4)
    UF_SYNC_WINDOW    minutos de ventana tras la hora (default 30)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import traceback
from datetime import datetime, timedelta

logger = logging.getLogger("uf.sync")

try:
    from chatbot.constants import CHILE_TZ
except Exception:  # pragma: no cover
    CHILE_TZ = None


def _feature_enabled() -> bool:
    return os.getenv("UF_SYNC_ENABLED", "true").lower() == "true"


def _sync_hour() -> int:
    return int(os.getenv("UF_SYNC_HOUR", "4"))


def _sync_window_minutes() -> int:
    return int(os.getenv("UF_SYNC_WINDOW", "30"))


def _now_local() -> datetime:
    if CHILE_TZ is not None:
        return datetime.now(CHILE_TZ)
    return datetime.now()


def _update_health_heartbeat(**extra) -> None:
    try:
        import webhook as _wh
        st = getattr(_wh, "background_tasks_status", None)
        if st is None:
            return
        st.setdefault("uf_sync", {"status": "starting", "last_heartbeat": None})
        st["uf_sync"]["last_heartbeat"] = _now_local().isoformat()
        st["uf_sync"].update(extra)
    except Exception:
        pass


def _persist_cycle_status(status: str, extra: dict | None = None) -> None:
    try:
        from .storage import get_db
        db = get_db()
        doc = {"updated_at": datetime.utcnow().isoformat(), "status": status}
        if extra:
            doc.update(extra)
        db["uf_sync_status"].update_one({"_id": "last"}, {"$set": doc}, upsert=True)
    except Exception:
        logger.error("[UF_SYNC] status persist failed:\n%s", traceback.format_exc())


def _next_run_at(now_local: datetime) -> datetime:
    today = now_local.replace(hour=_sync_hour(), minute=0, second=0, microsecond=0)
    if today > now_local:
        return today
    return today + timedelta(days=1)


async def _async_sleep_until_next_slot() -> None:
    now_local = _now_local()
    next_slot = _next_run_at(now_local)
    wait = max((next_slot - now_local).total_seconds(), 5)
    logger.info("[UF_SYNC] next cycle at %s (in %s)", next_slot.isoformat(), f"{int(wait)}s")
    await asyncio.sleep(wait)


def _due(now_local: datetime | None = None) -> bool:
    """True si estamos en la hora de corrida (dentro de la ventana)."""
    now = now_local or _now_local()
    if now.hour != _sync_hour():
        return False
    return now.minute <= _sync_window_minutes()


def run_uf_sync_cycle(db=None, force: bool = False, dry_run: bool = False) -> dict:
    """Un ciclo completo de sync UF. Devuelve métricas; nunca lanza."""
    metrics = {"status": "idle", "started_at": datetime.utcnow().isoformat()}
    if not _feature_enabled():
        _persist_cycle_status("disabled", {"reason": "flag_off"})
        return {"status": "disabled"}

    if not force and not _due():
        _persist_cycle_status("idle", {"reason": "outside_schedule"})
        return metrics

    if db is None:
        from .storage import get_db
        db = get_db()

    from .uf_service import (
        obtener_uf_actual, obtener_uf_cache_o_fallback,
        persistir_uf_cache, leer_uf_cache,
    )
    from .uf_migration import migrar

    # A. Obtener UF vigente UNA vez
    uf = obtener_uf_actual()
    if uf is None:
        # B. Fallback: conservar última UF válida; no modificar precios.
        cached = leer_uf_cache(db)
        metrics["status"] = "warning"
        metrics["reason"] = "mindicador_unavailable"
        metrics["uf"] = cached["valor"] if cached else None
        metrics["uf_fecha"] = cached.get("fecha") if cached else None
        metrics["conversiones"] = 0
        _persist_cycle_status("warning", {"reason": "mindicador_unavailable",
                                          "uf": metrics["uf"]})
        logger.warning("[UF_SYNC] Mindicador falló; UF previa conservada. "
                       "Próxima ejecución reintentará.")
        return metrics

    uf_valor = uf["valor"]
    uf_fecha = uf["fecha"]
    metrics["uf"] = uf_valor
    metrics["uf_fecha"] = uf_fecha

    if not dry_run:
        persistir_uf_cache(uf_valor, uf_fecha, fuente=uf.get("fuente", "mindicador.cl"), db=db)

    # C. Actualizar derivados de propiedades activas
    res = migrar(db, uf_valor, uf_fecha, dry_run=dry_run)
    metrics.update(res)
    metrics["status"] = "ok"
    _persist_cycle_status("ok", {"uf": uf_valor, "uf_fecha": uf_fecha,
                                 "conversiones": res["total_conversiones"]})
    logger.info("[UF_SYNC] ciclo ok: uf=%s conversiones=%s dry_run=%s",
                uf_valor, res["total_conversiones"], dry_run)
    return metrics


async def uf_sync_loop(sleep_seconds: int | None = None) -> None:
    """Loop diario. Feature-flagged. Un fallo en un ciclo no detiene otros."""
    if not _feature_enabled():
        _update_health_heartbeat(status="disabled")
        logger.info("[UF_SYNC] Disabled (UF_SYNC_ENABLED != true). Loop exit.")
        return

    _update_health_heartbeat(status="running")
    logger.info("[UF_SYNC] Loop started. hour=%s window=%smin",
                _sync_hour(), _sync_window_minutes())

    first = True
    while True:
        if not first:
            await _async_sleep_until_next_slot()
        first = False
        try:
            result = await asyncio.to_thread(run_uf_sync_cycle)
            _update_health_heartbeat(status="running", last_cycle=result.get("status"),
                                     uf=result.get("uf"))
        except Exception:
            _update_health_heartbeat(status="error")
            logger.error("[UF_SYNC] Loop cycle error:\n%s", traceback.format_exc())
        await _async_sleep_until_next_slot()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="UF sync diario")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    res = run_uf_sync_cycle(force=args.force, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, default=str, indent=2))
