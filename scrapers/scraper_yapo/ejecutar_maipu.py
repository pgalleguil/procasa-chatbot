"""Legacy Maipú command routed through the configurable search engine."""
from __future__ import annotations

import os
from pathlib import Path

from scrapers.scraper_yapo.ejecutar_busqueda import main as run_general_search


def main() -> int:
    profile = Path(os.environ.get("USERPROFILE") or Path.home())
    output_dir = profile / "Documents" / "PROCASA" / "YapoMaipu" / "general_search"
    return run_general_search([
        "--region", "Región Metropolitana",
        "--comuna", "Maipú",
        "--operacion", "venta",
        "--tipo", "casa",
        "--precio-min", "3000",
        "--precio-max", "6000",
        "--moneda", "UF",
        "--output-dir", str(output_dir),
        "--live",
    ])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
