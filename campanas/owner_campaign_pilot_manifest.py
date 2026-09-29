"""Build a read-only, batch-specific campaign manifest from a code selection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from campanas.owner_campaign_live_prepare import BASE_DIR, prepare_manifest_from_selection


DEFAULT_SELECTION = BASE_DIR / "reports" / "owner_campaign_sucre_wave1_pilot10_selection.csv"
DEFAULT_OUTPUT = BASE_DIR / "reports" / "owner_campaign_sucre_wave1_pilot10_20260929.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a selected owner-campaign manifest without sending")
    parser.add_argument("--selection", default=str(DEFAULT_SELECTION))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    result = prepare_manifest_from_selection(args.selection, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
