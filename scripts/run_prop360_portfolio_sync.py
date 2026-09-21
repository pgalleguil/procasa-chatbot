"""Punto de entrada manual y seguro para actualizar la cartera Prop360.

Uso desde la raíz del proyecto:

    python scripts/run_prop360_portfolio_sync.py
    python scripts/run_prop360_portfolio_sync.py --office-id 7
    python scripts/run_prop360_portfolio_sync.py --execute
    python scripts/run_prop360_portfolio_sync.py --execute --office-id 7

Sin ``--execute`` sólo se ejecuta un dry-run. Este archivo no inicia ningún
scheduler y no ejecuta ``ficha_sync_loop``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.sucre_portfolio_sync import run_portfolio_operational_sync


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prevalidar o actualizar manualmente la cartera Prop360 PROCASA."
    )
    parser.add_argument(
        "--office-id",
        type=int,
        default=None,
        help="Oficina específica; 7 corresponde a PROCASA SUCRE. Por defecto: todas las PROCASA.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Aplica cambios en MongoDB. Sin esta opción sólo hace dry-run.",
    )
    parser.add_argument(
        "--apply-bajas",
        action="store_true",
        help="Aplica bajas confirmadas. Sólo permitido junto con --execute.",
    )
    args = parser.parse_args()

    if args.apply_bajas and not args.execute:
        parser.error("--apply-bajas requiere --execute")

    result = run_portfolio_operational_sync(
        office_id=args.office_id,
        dry_run=not args.execute,
        apply_bajas=args.apply_bajas,
        generate_embeddings=args.execute,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in {"completed", "running"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
