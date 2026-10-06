"""Backward-compatible entry point for the first 2026-09 context snapshot."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from owner_portal.market_context import SEPTEMBER_2026_PERIOD, SEPTEMBER_2026_SEED  # noqa: E402
from scripts.update_market_context_snapshots import (  # noqa: E402
    _mongo_config,
    _print_manifest,
    _print_plan,
    run_update,
    validate_seed,
)


def print_manifest(documents=SEPTEMBER_2026_SEED) -> bool:
    return _print_manifest(list(documents), SEPTEMBER_2026_PERIOD)


def run(*, execute: bool = False) -> int:
    return run_update(period=SEPTEMBER_2026_PERIOD, execute=execute)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="write the September snapshot; default is read-only")
    args = parser.parse_args()
    return run(execute=args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
