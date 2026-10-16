#!/usr/bin/env python
"""Dry-run-first entrypoint for the 25-property Sucre publication worker."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.publication_sync import DEFAULT_BATCH_SIZE, run_publication_sync_batch


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Run one bounded PROCASA SUCRE publication-sync batch.")
    parser.add_argument("--office-id", type=int, default=None)
    parser.add_argument("--execute", action="store_true", help="Persist publication-only changes; default is dry-run.")
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--backup-dir", default=os.getenv("PUBLICATION_SYNC_BACKUP_DIR"))
    parser.add_argument("--output-dir", default=os.getenv("PUBLICATION_SYNC_REPORT_DIR"))
    args = parser.parse_args(argv)
    if args.office_id != 7:
        print("ERROR: --office-id 7 is required; no other office is in scope.", file=sys.stderr)
        return 2
    if args.execute and not args.backup_dir:
        print("ERROR: --execute requires --backup-dir or PUBLICATION_SYNC_BACKUP_DIR on durable private storage.", file=sys.stderr)
        return 2
    try:
        result = run_publication_sync_batch(
            dry_run=not args.execute, execute=args.execute, office_id=7,
            batch_size=args.limit, backup_dir=args.backup_dir, output_dir=args.output_dir,
        )
    except Exception as exc:
        print(f"PUBLICATION_SYNC_FAILED={type(exc).__name__}:{str(exc)[:200]}", file=sys.stderr)
        return 1
    summary = {key: result.get(key) for key in (
        "run_id", "office_id", "dry_run", "execute", "total_active", "checked",
        "changed", "unchanged", "new_urls", "historical_urls_preserved",
        "missing_urls", "unverified", "conflicts", "errors", "mongo_writes",
        "documents_updated", "backup", "report_json", "report_csv",
    )}
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
