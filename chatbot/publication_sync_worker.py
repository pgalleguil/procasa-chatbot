"""Opt-in periodic runner for the isolated PROCASA Sucre publication sync.

This module is intentionally not imported by webhook/application startup. Run
it as a separate supervised worker after configuring its explicit environment
flags and a durable private backup directory.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

from chatbot.publication_sync import DEFAULT_BATCH_SIZE, run_publication_sync_batch

logger = logging.getLogger("prop360.publication_sync_worker")


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def run_worker(*, execute: bool, interval_seconds: int,
               backup_dir: str | Path | None = None,
               output_dir: str | Path | None = None,
               max_cycles: int | None = None,
               batch_runner: Callable[..., dict] = run_publication_sync_batch,
               sleep: Callable[[float], None] = time.sleep) -> int:
    if interval_seconds < 30:
        raise ValueError("interval_seconds_must_be_at_least_30")
    if execute and not backup_dir:
        raise ValueError("execute_requires_durable_private_backup_dir")
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        try:
            result = batch_runner(
                dry_run=not execute, execute=execute, office_id=7,
                batch_size=DEFAULT_BATCH_SIZE, backup_dir=backup_dir,
                output_dir=output_dir,
            )
            logger.info("[PUBLICATION_SYNC_WORKER] %s", json.dumps({
                key: result.get(key) for key in (
                    "run_id", "status", "dry_run", "execute", "total_active",
                    "selected_active", "checked", "changed", "unchanged",
                    "conflicts", "errors", "inventory_mismatches",
                    "mongo_writes", "documents_updated", "backup",
                )
            }, ensure_ascii=False, default=str))
        except Exception as exc:
            # Keep one source/API failure from terminating future reconciliations.
            # Do not log request bodies, credentials, or property owner data.
            logger.error("[PUBLICATION_SYNC_WORKER] cycle_failed=%s", type(exc).__name__)
        cycles += 1
        if max_cycles is None or cycles < max_cycles:
            sleep(interval_seconds)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Opt-in PROCASA Sucre publication sync worker")
    parser.add_argument("--interval-seconds", type=int,
                        default=int(os.getenv("PUBLICATION_SYNC_INTERVAL_SECONDS", "1200")))
    parser.add_argument("--once", action="store_true", help="Run one batch and exit")
    args = parser.parse_args(argv)
    if not _enabled(os.getenv("PUBLICATION_SYNC_ENABLED")):
        print("PUBLICATION_SYNC_ENABLED=NO; worker remains inactive")
        return 0
    execute = _enabled(os.getenv("PUBLICATION_SYNC_EXECUTE"))
    backup_dir = os.getenv("PUBLICATION_SYNC_BACKUP_DIR")
    if execute and not backup_dir:
        parser.error("PUBLICATION_SYNC_EXECUTE requires PUBLICATION_SYNC_BACKUP_DIR")
    output_dir = os.getenv("PUBLICATION_SYNC_REPORT_DIR")
    return run_worker(
        execute=execute,
        interval_seconds=args.interval_seconds,
        backup_dir=backup_dir,
        output_dir=output_dir,
        max_cycles=1 if args.once else None,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
