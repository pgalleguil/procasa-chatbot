#!/usr/bin/env python
"""Dry-run-first Prop360 publication reconciler restricted to PROCASA SUCRE."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.publication_sync import (
    DEFAULT_BATCH_SIZE,
    run_publication_reconciliation,
    run_publication_sync_batch,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit/sync publication URLs for active PROCASA SUCRE properties only."
    )
    parser.add_argument("--office-id", type=int, default=None,
                        help="Must be 7 (PROCASA SUCRE); required for --execute.")
    parser.add_argument("--execute", action="store_true",
                        help="Persist only publication fields; dry-run is the default.")
    parser.add_argument("--codigo", action="append", dest="codes",
                        help="Restrict to an exact active Prop360 code; repeatable.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit the run. --execute is restricted to 1–25 properties.")
    parser.add_argument("--batch", action="store_true",
                        help="Use persistent checkpoint and due-Proppit-retry selection.")
    parser.add_argument("--output-dir", default=None,
                        help="Local JSON/CSV report directory (no Mongo writes in dry-run).")
    parser.add_argument("--backup-dir", default=None,
                        help="Private local backup directory for --execute.")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    office_id = args.office_id if args.office_id is not None else 7
    if office_id != 7:
        print("ERROR: this command is restricted to --office-id 7 (PROCASA SUCRE).", file=sys.stderr)
        return 2
    if args.execute:
        if args.office_id != 7:
            print("ERROR: --execute requires explicit --office-id 7.", file=sys.stderr)
            return 2
        if args.batch and args.codes:
            print("ERROR: choose --batch or explicit --codigo values, not both.", file=sys.stderr)
            return 2
        if not args.batch and (args.limit is None or not 1 <= args.limit <= DEFAULT_BATCH_SIZE):
            print("ERROR: --execute requires --limit from 1 to 25; use --batch for the checkpointed worker.", file=sys.stderr)
            return 2
    try:
        if args.batch:
            result = run_publication_sync_batch(
                dry_run=not args.execute, execute=args.execute,
                office_id=office_id, batch_size=args.limit or DEFAULT_BATCH_SIZE,
                backup_dir=args.backup_dir, output_dir=args.output_dir,
            )
        else:
            result = run_publication_reconciliation(
                dry_run=not args.execute, execute=args.execute,
                office_id=office_id, limit=args.limit, codes=args.codes,
                output_dir=args.output_dir, backup_dir=args.backup_dir,
            )
    except Exception as exc:
        print(f"PUBLICATION_SYNC_FAILED={type(exc).__name__}:{str(exc)[:200]}", file=sys.stderr)
        return 1
    summary = {key: result.get(key) for key in (
        "run_id", "office_id", "dry_run", "execute", "total_active", "checked",
        "changed", "unchanged", "new_urls", "historical_urls_preserved",
        "missing_urls", "unverified", "conflicts", "errors", "mongo_writes",
        "backup", "report_json", "report_csv",
    )}
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
