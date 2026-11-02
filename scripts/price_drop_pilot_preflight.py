"""Read-only CRM staging report; intentionally has NO send/send-on-start flag.

Usage: python -m scripts.price_drop_pilot_preflight
Does not mutate CRM, call WhatsApp, or schedule follow-ups.
"""
from __future__ import annotations

import argparse
import json

from chatbot.price_drop_pilot_staging import CODE, preview_candidates
from chatbot.storage import get_db


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview a blocked, read-only price-drop candidate shortlist")
    parser.add_argument("--property", default=CODE, help="Verified Prop360 property code")
    args = parser.parse_args()
    result = preview_candidates(get_db(), code=args.property)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") == "PREVIEW_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
