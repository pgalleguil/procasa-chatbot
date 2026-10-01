"""Disabled compatibility module for the former automatic ficha scheduler.

Prop360 portfolio refreshes are performed by the local CLI in
`scraping_convecta/scraping_prop360_ficha_completa.py` only.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("ficha.sync")


def run_ficha_sync_cycle(db=None) -> dict:
    """Former scheduled writer is permanently disabled; performs no I/O."""
    logger.warning("[FICHA_SYNC] Automatic portfolio sync disabled; no reads or writes")
    return {"status": "disabled", "portfolio_reads": 0, "portfolio_writes": 0}


async def ficha_sync_loop(sleep_seconds: int | None = None) -> None:
    """Compatibility no-op: no timer, polling, or background work is started."""
    logger.info("[FICHA_SYNC] Automatic scheduler disabled; use the local manual scraper")
    return None
