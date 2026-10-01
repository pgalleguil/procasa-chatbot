"""Explicit one-shot UF cache refresh; no scheduler and no portfolio writes."""
from __future__ import annotations

import argparse
import json
import logging

logger = logging.getLogger("uf.cache_refresh")


def run_uf_sync_cycle(db=None, force: bool = False, dry_run: bool = True,
                      uf_info: dict | None = None) -> dict:
    """Validate one official UF quote; optionally update only `uf_cache`.

    ``force`` is accepted for old callers but has no scheduling semantics.
    Dry-run is the safe default. This function never reads or writes property
    documents and is not started by the web application.
    """
    from .uf_service import obtener_uf_actual, persistir_uf_cache, validar_uf_vigente

    quote = uf_info if uf_info is not None else obtener_uf_actual()
    valid, reason = validar_uf_vigente(quote)
    if not valid or not isinstance(quote, dict) or quote.get("fuente") != "mindicador.cl":
        return {
            "status": "aborted",
            "reason": reason if not valid else "uf_source_invalid",
            "uf": None,
            "uf_fecha": None,
            "portfolio_reads": 0,
            "portfolio_writes": 0,
            "uf_cache_writes": 0,
        }
    result = {
        "status": "dry_run" if dry_run else "ok",
        "uf": quote["valor"],
        "uf_fecha": quote["fecha"],
        "source": quote["fuente"],
        "portfolio_reads": 0,
        "portfolio_writes": 0,
        "uf_cache_writes": 0,
    }
    if not dry_run:
        if db is None:
            from .storage import get_db
            db = get_db()
        persistir_uf_cache(quote["valor"], quote["fecha"], fuente=quote["fuente"], db=db)
        result["uf_cache_writes"] = 1
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Actualización explícita de uf_cache")
    parser.add_argument("--execute", action="store_true", help="escribir solo uf_cache")
    args = parser.parse_args(argv)
    result = run_uf_sync_cycle(dry_run=not args.execute)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 2 if result.get("status") == "aborted" else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
