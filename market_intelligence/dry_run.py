from __future__ import annotations

import json

from .sources import run_dry_run


def main() -> int:
    print(json.dumps(run_dry_run(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
