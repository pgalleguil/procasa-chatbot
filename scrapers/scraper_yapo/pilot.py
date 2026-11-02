from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

_SCRAPER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRAPER_DIR.parent.parent))
sys.path.insert(0, str(_SCRAPER_DIR.parent))

from classifier_rules import classify_with_rules
from config import AppConfig
from discovery import discover_listing_urls, normalize_yapo_url
from downloader import download_html
from extractor import extract_listing_fields
from mongo_store import MongoStore
from proxy_manager import ProxyManager


PILOT_SCOPE = {"portal": "Yapo.cl", "operation": "venta", "property_type": "casa", "commune": "Maipú", "price_uf_min": 3000, "price_uf_max": 6000}


def _listing_id(url: str) -> str:
    match = re.search(r"/(\d{6,})(?:[/?#]|$)", url)
    return match.group(1) if match else ""


def _scope_url_is_valid(url: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    q_value = " ".join(query.get("q", [])).lower()
    region = " ".join(query.get("regionslug", [])).lower()
    if parsed.hostname not in {"www.yapo.cl", "yapo.cl"}:
        return False, "start_url_no_es_yapo"
    if not parsed.path.startswith("/searchresult/bienes-raices-venta-de-propiedades"):
        return False, "ruta_no_confirma_busqueda_de_venta"
    if "casas" not in q_value or "bienes-raices-venta-de-propiedades-casas" not in q_value:
        return False, "filtro_casa_no_confirmado_en_q"
    if "maipu" not in region:
        return False, "comuna_maipu_no_confirmada_en_regionslug"
    return True, "operation_house_commune_filters_present"


def _price_in_uf(raw: str) -> float | None:
    if not raw or not re.search(r"\bUF\b", raw, re.I):
        return None
    match = re.search(r"(?:UF\s*)?([\d.]+(?:,\d+)?)\s*UF?", raw, re.I)
    if not match:
        match = re.search(r"UF\s*([\d.]+(?:,\d+)?)", raw, re.I)
    if not match:
        return None
    try:
        return float(match.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_pilot(start_url: str, config: AppConfig | None = None, *, batch_id: str | None = None) -> dict[str, Any]:
    config = config or AppConfig()
    config.ensure_layout()
    batch_id = batch_id or config.generate_batch_id()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary: dict[str, Any] = {
        "scope": PILOT_SCOPE.copy(), "batch_id": batch_id, "pages_visited": 0,
        "price_filter_mode": "local_only_from_explicit_uf_price; no_clp_conversion",
        "pagination_complete": False, "pagination_cause": "not_started",
        "ads_discovered": 0, "batch_duplicates": 0, "mongo_existing": 0,
        "new_ads": 0, "filter_matches": 0, "pending_price_validation": 0,
        "errors_or_blocks": [], "proxy_requests": 0, "deepseek_calls": 0,
        "mongo_writes": 0, "mongo_dedup_verified": False,
        "mongo_dedup_status": "not_attempted", "search_filters_verified": False,
    }
    audit: list[dict[str, Any]] = []
    valid_scope, scope_reason = _scope_url_is_valid(start_url)
    if not valid_scope:
        summary["errors_or_blocks"].append(f"search_scope_unverified:{scope_reason}")
        summary["pagination_cause"] = "search_scope_unverified"
    else:
        proxy_manager = ProxyManager.from_config(config)
        if not proxy_manager.has_proxies():
            summary["errors_or_blocks"].append("proxy_unavailable; direct network access blocked")
            summary["pagination_cause"] = "proxy_unavailable"
        else:
            discovery_report: dict[str, Any] = {}
            records = discover_listing_urls([start_url], max_pages=None, max_urls=None, until_end=True,
                                            batch_id=batch_id, target_communes=["Maipú"], report=discovery_report)
            summary["search_filters_verified"] = bool(records) and all(
                "maipu" in str(row.get("discovery_comuna") or "").lower().replace("ú", "u")
                and "/bienes-raices-venta-de-propiedades-casas/" in str(row.get("url") or "").lower()
                for row in records
            )
            summary["pages_visited"] = len(discovery_report.get("pages", []))
            summary["pagination_complete"] = bool(discovery_report.get("pagination_complete"))
            summary["pagination_cause"] = discovery_report.get("pagination_cause", "unknown")
            summary["page_log"] = discovery_report.get("pages", [])
            summary["ads_discovered"] = sum(int(page.get("listing_count", 0)) for page in discovery_report.get("pages", []))
            summary["proxy_requests"] += int(discovery_report.get("proxy_requests", 0))
            summary["errors_or_blocks"].extend(discovery_report.get("errors", []))
            unique: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            seen_urls: set[str] = set()
            summary["batch_duplicates"] = sum(int(page.get("duplicate_count", 0)) for page in discovery_report.get("pages", []))
            for record in records:
                normalized_url = normalize_yapo_url(record.get("url", ""))
                if "/bienes-raices-venta-de-propiedades-casas/" not in normalized_url.lower():
                    summary["errors_or_blocks"].append("out_of_scope_result_skipped_before_detail")
                    continue
                listing_id = _listing_id(normalized_url)
                if normalized_url in seen_urls or (listing_id and listing_id in seen_ids):
                    summary["batch_duplicates"] += 1
                    continue
                seen_urls.add(normalized_url)
                if listing_id:
                    seen_ids.add(listing_id)
                unique.append({**record, "url": normalized_url, "listing_id": listing_id})
            try:
                if not config.mongo_uri:
                    raise RuntimeError("MONGO_URI no configurado")
                ids = [str(row["listing_id"]) for row in unique if row["listing_id"]]
                if not ids:
                    existing_ids = set()
                    summary["mongo_dedup_status"] = "not_attempted_no_discovered_ids"
                else:
                    store = MongoStore(config)
                    existing_ids = store.existing_listing_ids(ids)
                    summary["mongo_dedup_verified"] = True
                    summary["mongo_dedup_status"] = "verified_read_only"
            except Exception as exc:
                existing_ids = set()
                summary["mongo_dedup_status"] = "unverified_unavailable"
                summary["errors_or_blocks"].append(f"mongo_dedup_unverified:{type(exc).__name__}")
            new_records = [row for row in unique if summary["mongo_dedup_verified"] and row["listing_id"] not in existing_ids]
            summary["mongo_existing"] = len(unique) - len(new_records) if summary["mongo_dedup_verified"] else 0
            summary["new_ads"] = len(new_records)
            # If Mongo is unavailable, do not treat records as new or download their details.
            for record in new_records:
                try:
                    downloaded = download_html(record["url"], config, proxy_manager, batch_id=batch_id)
                    summary["proxy_requests"] += int(downloaded.fetch_source == "requests_proxy")
                    if downloaded.blocked:
                        summary["errors_or_blocks"].append(f"listing_blocked:{record['listing_id']}")
                        break
                    extracted = extract_listing_fields(downloaded.html, source_url=record["url"])
                except Exception as exc:
                    summary["errors_or_blocks"].append(f"detail_error:{record['listing_id']}:{type(exc).__name__}")
                    if "BLOCKED" in str(exc):
                        break
                    continue
                title = extracted.get("title", "")
                description = extracted.get("description", extracted.get("descripcion", ""))
                raw_price = str(extracted.get("price") or "")
                price_uf = _price_in_uf(raw_price)
                comuna = extracted.get("comuna") or record.get("discovery_comuna") or ""
                operation = "venta" if "venta" in record["url"].lower() else ""
                property_type = "casa" if "casa" in record["url"].lower() else ""
                local_match = (
                    "maipu" in comuna.lower().replace("ú", "u")
                    and operation == "venta" and property_type == "casa"
                    and price_uf is not None and 3000 <= price_uf <= 6000
                )
                if price_uf is None:
                    summary["pending_price_validation"] += 1
                if local_match:
                    summary["filter_matches"] += 1
                classification = classify_with_rules(extracted)
                if classification.get("state") == "INCONCLUSIVE":
                    classification = {**classification, "state": "INCIERTO", "reason": "Evidencia insuficiente; clasificación preliminar pendiente de revisión."}
                audit.append({
                    "listing_id": record["listing_id"], "url": record["url"],
                    "discovered_page": record.get("page_number"), "title": title,
                    "description": description, "comuna": comuna,
                    "description_summary": extracted.get("description_summary", ""),
                    "tipo_propiedad": property_type, "operacion": operation,
                    "precio_raw": raw_price, "precio_uf": price_uf,
                    "precio_validacion": "pendiente_sin_valor_uf_verificado" if price_uf is None else "precio_uf_observado",
                    "publicador_visible": extracted.get("publicador_visible", ""),
                    "tipo_anunciante": extracted.get("seller_type", ""),
                    "logo": extracted.get("contact_logo_alt", ""),
                    "perfil": extracted.get("seller_profile_id", ""),
                    "clasificacion_preliminar": classification,
                    "evidencia": classification.get("evidence", []),
                    "problemas_extraccion": (
                        ["full_description_missing; meta_summary_kept_separately"]
                        if not description and extracted.get("description_summary")
                        else ["description_marked_truncated"] if extracted.get("descripcion_is_truncated")
                        else ["full_description_missing"] if not description else []
                    ),
                    "filter_match": local_match,
                })
    summary["calls_to_deepseek"] = 0
    summary["mongo_writes"] = 0
    summary_path = config.reports_dir / f"yapo_maipu_pilot_summary_{timestamp}.json"
    audit_path = config.reports_dir / f"yapo_maipu_pilot_audit_{timestamp}.json"
    _write_json(summary_path, summary)
    _write_json(audit_path, audit)
    summary["summary_report"] = str(summary_path)
    summary["audit_report"] = str(audit_path)
    return summary
