"""General Yapo search runner; simulation is the default and writes are absent."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scrapers.scraper_yapo.config import AppConfig
from scrapers.scraper_yapo.pilot import run_search
from scrapers.scraper_yapo.search_config import SearchConfig, SearchConfigurationError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Búsqueda Yapo general, con simulación offline por defecto y sin escrituras a MongoDB."
    )
    parser.add_argument("--region", required=True, help="Nombre de región; por ejemplo: Región Metropolitana")
    parser.add_argument("--comuna", action="append", required=True,
                        help="Comuna; se puede repetir o indicar separadas por coma")
    parser.add_argument("--todas-comunas-region", action="store_true",
                        help="Solicitar todas las comunas; se bloquea hasta contar con verificación Yapo completa")
    parser.add_argument("--operacion", required=True, choices=("venta", "arriendo", "alquiler"))
    parser.add_argument("--tipo", required=True,
                        help="casa, departamento, terreno, parcela, local comercial, oficina, edificio, negocio o estacionamiento")
    parser.add_argument("--precio-min", dest="price_min")
    parser.add_argument("--precio-max", dest="price_max")
    parser.add_argument("--moneda", dest="price_currency", choices=("UF", "CLP", "CLF"))
    parser.add_argument("--dormitorios-min")
    parser.add_argument("--dormitorios-max")
    parser.add_argument("--banos-min")
    parser.add_argument("--banos-max")
    parser.add_argument("--superficie-min-m2")
    parser.add_argument("--superficie-max-m2")
    parser.add_argument("--max-pages", type=int, help="Límite de páginas; si corta resultados, la corrida queda incompleta")
    parser.add_argument("--max-listings", type=int, help="Límite de anuncios conservados; la paginación continúa hasta el final o --max-pages y el informe marca la selección como truncada")
    parser.add_argument("--output-dir", default="", help="Carpeta de reportes; por defecto scrapers/scraper_yapo/reports/general_search")
    parser.add_argument("--batch-id", default="", help="Identificador corto del lote")
    parser.add_argument("--uf-valor-clp", help="Valor CLP por UF para comparar anuncios en moneda distinta")
    parser.add_argument("--uf-fecha", help="Fecha ISO del valor UF entregado, YYYY-MM-DD")
    parser.add_argument("--uf-fuente", default="", help="Fuente documentada del valor UF utilizado")
    parser.add_argument("--live", action="store_true",
                        help="Habilita páginas Yapo solo a través de FlashProxy y lecturas Mongo para deduplicar")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.todas_comunas_region:
        parser.error("Todas las comunas de una región no está habilitado: falta verificación del catálogo completo en Yapo.")
    try:
        search = SearchConfig(
            region=args.region,
            communes=tuple(args.comuna),
            operation=args.operacion,
            property_type=args.tipo,
            price_min=args.price_min,
            price_max=args.price_max,
            price_currency=args.price_currency,
            bedrooms_min=args.dormitorios_min,
            bedrooms_max=args.dormitorios_max,
            bathrooms_min=args.banos_min,
            bathrooms_max=args.banos_max,
            surface_min_m2=args.superficie_min_m2,
            surface_max_m2=args.superficie_max_m2,
            max_pages=args.max_pages,
            max_listings=args.max_listings,
            output_dir=args.output_dir,
            batch_id=args.batch_id,
            live=args.live,
        )
    except SearchConfigurationError as exc:
        parser.error(str(exc))

    config = AppConfig()
    if args.uf_valor_clp:
        try:
            config.uf_valor_clp = float(args.uf_valor_clp)
        except ValueError:
            parser.error("--uf-valor-clp debe ser numérico.")
    if args.uf_fecha:
        config.uf_fecha = args.uf_fecha
    if args.uf_fuente:
        config.uf_source = args.uf_fuente
    result = run_search(search, config)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    complete_live_result = not search.live or bool(result.get("commercial_result_available"))
    return 0 if not result.get("errors_or_blocks") and complete_live_result else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
