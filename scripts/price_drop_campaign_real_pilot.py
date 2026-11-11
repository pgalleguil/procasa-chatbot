"""Deprecated entry point retained only to fail safely for old instructions."""
from __future__ import annotations

import json


def main(argv=None) -> int:
    print(json.dumps({
        "status": "BLOCKED",
        "reason": "legacy_campaign_sender_removed_use_scripts_enviar_piloto_rebajas",
    }, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
