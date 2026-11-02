from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
os.chdir(REPOSITORY_ROOT)

# This entry point never calls an AI service. Override even .env values before
# importing the scraper configuration so DeepSeek stays disabled for this run.
os.environ["DEEPSEEK_ENABLED"] = "false"
os.environ["DEEPSEEK_API_KEY"] = ""

from scrapers.scraper_yapo.config import AppConfig
from scrapers.scraper_yapo.diagnose_proxy import diagnose_proxy
from scrapers.scraper_yapo.mongo_store import MongoStore
from scrapers.scraper_yapo.pilot import run_pilot
from scrapers.scraper_yapo.proxy_manager import ProxyManager


START_URL = (
    "https://www.yapo.cl/"
    "bienes-raices-venta-de-propiedades-casas/region-metropolitana-maipu"
)

CSV_FIELDS = (
    "listing_id",
    "url",
    "discovered_page",
    "title",
    "description",
    "comuna",
    "discovery_comuna",
    "validacion_comuna",
    "tipo_propiedad",
    "operacion",
    "precio_raw",
    "precio_uf",
    "precio_validacion",
    "publicador_visible",
    "tipo_anunciante",
    "logo",
    "perfil",
    "clasificacion_preliminar",
    "evidencia",
    "problemas_extraccion",
    "filter_match",
)


def _documents_dir() -> Path:
    profile = Path(os.environ.get("USERPROFILE") or Path.home())
    return profile / "Documents" / "PROCASA" / "YapoMaipu"


def _csv_row(record: dict[str, Any]) -> dict[str, str]:
    classification = record.get("clasificacion_preliminar") or {}
    return {
        "listing_id": str(record.get("listing_id") or ""),
        "url": str(record.get("url") or ""),
        "discovered_page": str(record.get("discovered_page") or ""),
        "title": str(record.get("title") or ""),
        "description": str(record.get("description") or ""),
        "comuna": str(record.get("comuna") or ""),
        "discovery_comuna": str(record.get("discovery_comuna") or ""),
        "validacion_comuna": str(record.get("validacion_comuna") or ""),
        "tipo_propiedad": str(record.get("tipo_propiedad") or ""),
        "operacion": str(record.get("operacion") or ""),
        "precio_raw": str(record.get("precio_raw") or ""),
        "precio_uf": str(record.get("precio_uf") or ""),
        "precio_validacion": str(record.get("precio_validacion") or ""),
        "publicador_visible": str(record.get("publicador_visible") or ""),
        "tipo_anunciante": str(record.get("tipo_anunciante") or ""),
        "logo": str(record.get("logo") or ""),
        "perfil": str(record.get("perfil") or ""),
        "clasificacion_preliminar": json.dumps(
            classification, ensure_ascii=False, separators=(",", ":")
        ),
        "evidencia": json.dumps(
            record.get("evidencia") or [], ensure_ascii=False, separators=(",", ":")
        ),
        "problemas_extraccion": json.dumps(
            record.get("problemas_extraccion") or [],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "filter_match": str(bool(record.get("filter_match"))).lower(),
    }


def _write_new_ads_csv(records: list[dict[str, Any]], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    destination = output_dir / f"yapo_maipu_nuevos_{timestamp}.csv"
    with destination.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(_csv_row(record) for record in records)
    return destination


def _load_audit(summary: dict[str, Any]) -> list[dict[str, Any]]:
    audit_path = Path(str(summary.get("audit_report") or ""))
    if not audit_path.is_file():
        raise RuntimeError("No se encontr\u00f3 el reporte local de avisos nuevos.")
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("El reporte local de avisos tiene un formato inesperado.")
    return payload


def run() -> int:
    config = AppConfig()
    config.ensure_layout()

    if not config.mongo_uri:
        print("ERROR: falta MONGO_URI; no se consult\u00f3 Yapo ni se marcaron avisos como nuevos.")
        return 2

    proxy_manager = ProxyManager.from_config(config)
    if not proxy_manager.has_proxies():
        print("ERROR: no hay proxies configurados; no se permite conexi\u00f3n directa.")
        return 2

    print("Comprobando DNS, TCP y solicitud controlada mediante el proxy configurado...")
    proxy_report = diagnose_proxy(config)
    if proxy_report.get("result_category") != "OK":
        category = str(proxy_report.get("result_category") or "UNKNOWN")
        report_name = str(proxy_report.get("report_file") or "")
        print(f"ERROR: preflight del proxy fall\u00f3 ({category}); no se solicit\u00f3 ninguna p\u00e1gina de Yapo.")
        if report_name:
            print(f"Diagn\u00f3stico sanitizado: {config.reports_dir / report_name}")
        return 2

    # Guard against an accidental future write being added to the pilot path.
    def deny_mongo_write(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("Escritura MongoDB bloqueada por ejecutar_maipu.py.")

    MongoStore.upsert_listing = deny_mongo_write  # type: ignore[method-assign]

    print("Proxy disponible. Recorriendo todas las p\u00e1ginas de la b\u00fasqueda de Maip\u00fa...")
    summary = run_pilot(START_URL, config)
    pages = summary.get("pages_visited") or 0
    complete = bool(summary.get("pagination_complete"))
    dedup_verified = bool(summary.get("mongo_dedup_verified"))
    discovered = summary.get("ads_discovered")

    if not complete:
        cause = str(summary.get("pagination_cause") or "unknown")
        first_page = next(
            (item for item in summary.get("page_log", [])
             if item.get("page_number") == 1),
            {},
        )
        print(
            "ERROR: la paginaci\u00f3n no se verific\u00f3 hasta el final; "
            f"no se export\u00f3 el CSV. P\u00e1ginas visitadas: {pages}."
        )
        print(f"CAUSA DE PAGINACI\u00d3N: {cause}")
        print(
            "P\u00c1GINA 1: "
            f"estado={first_page.get('status', 'no_registrada')}, "
            f"enlaces_encontrados={first_page.get('listing_count')}, "
            f"enlaces_nuevos={first_page.get('new_count')}, "
            f"enlaces_duplicados={first_page.get('duplicate_count')}, "
            f"anchors_html={first_page.get('anchors_total')}, "
            f"validacion={first_page.get('validation', 'no_aplica')}"
        )
        print(
            "RED: "
            f"descargas_proxy={summary.get('successful_downloads', 0)}, "
            f"cache={summary.get('cache_responses', 0)}, "
            f"solicitudes_intentadas={summary.get('request_attempts', 0)}"
        )
        print(f"HTML LOCAL: {config.html_dumps_dir / str(summary.get('batch_id') or '')}")
        print(f"Resumen: {summary.get('summary_report', '')}")
        return 3
    if discovered not in (0, None) and not dedup_verified:
        print("ERROR: MongoDB no verific\u00f3 la deduplicaci\u00f3n; no se export\u00f3 el CSV.")
        print(f"Resumen: {summary.get('summary_report', '')}")
        return 4
    if discovered is None:
        print("ERROR: no hay conteo verificable de avisos; no se export\u00f3 el CSV.")
        print(f"Resumen: {summary.get('summary_report', '')}")
        return 4
    if discovered and not summary.get("search_filters_verified"):
        print("ERROR: los filtros del resultado no pudieron verificarse; no se export\u00f3 el CSV.")
        print(f"Resumen: {summary.get('summary_report', '')}")
        return 5

    new_ads = _load_audit(summary)
    csv_path = _write_new_ads_csv(new_ads, _documents_dir())
    print(
        json.dumps(
            {
                "pages_visited": pages,
                "pagination_complete": complete,
                "ads_discovered": discovered,
                "batch_duplicates": summary.get("batch_duplicates"),
                "mongo_existing": summary.get("mongo_existing"),
                "mongo_dedup_verified": dedup_verified,
                "new_ads_analyzed": len(new_ads),
                "filter_matches": summary.get("filter_matches"),
                "csv": str(csv_path),
                "deepseek_calls": 0,
                "mongo_writes": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Barrido local completo de casas en venta en Maip\u00fa; "
            "deduplicaci\u00f3n MongoDB de solo lectura y CSV privado."
        )
    )
    parser.parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())

