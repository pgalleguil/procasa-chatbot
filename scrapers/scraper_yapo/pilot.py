"""Read-only search runner shared by Maipú and all configured Yapo searches."""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_SCRAPER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRAPER_DIR.parent.parent))
sys.path.insert(0, str(_SCRAPER_DIR.parent))

try:
    from .classifier_rules import classify_yapo_batch, classify_yapo_listing
    from .config import AppConfig
    from .discovery import discover_listing_urls, normalize_yapo_url
    from .downloader import assess_yapo_availability, download_html, html_path_for_url, validate_html
    from .enrich import _enrich_property_fields
    from .extractor import extract_listing_fields
    from .mongo_store import MongoStore
    from .proxy_manager import ProxyManager
    from .publisher_profile import enrich_batch_publisher_profiles
    from .search_config import (
        FILTER_EVIDENCE,
        PROPERTY_CATEGORIES,
        SearchConfig,
        SearchConfigurationError,
        normalize_region_slug,
        search_path_matches_config,
        search_config_from_url,
        verify_search_page,
    )
except ImportError:  # direct script execution
    from scrapers.scraper_yapo.classifier_rules import classify_yapo_batch, classify_yapo_listing
    from scrapers.scraper_yapo.config import AppConfig
    from scrapers.scraper_yapo.discovery import discover_listing_urls, normalize_yapo_url
    from scrapers.scraper_yapo.downloader import assess_yapo_availability, download_html, html_path_for_url, validate_html
    from scrapers.scraper_yapo.enrich import _enrich_property_fields
    from scrapers.scraper_yapo.extractor import extract_listing_fields
    from scrapers.scraper_yapo.mongo_store import MongoStore
    from scrapers.scraper_yapo.proxy_manager import ProxyManager
    from scrapers.scraper_yapo.publisher_profile import enrich_batch_publisher_profiles
    from scrapers.scraper_yapo.search_config import (
        FILTER_EVIDENCE,
        PROPERTY_CATEGORIES,
        SearchConfig,
        SearchConfigurationError,
        normalize_region_slug,
        search_path_matches_config,
        search_config_from_url,
        verify_search_page,
    )


def _listing_id(url: str) -> str:
    match = re.search(r"/(\d{6,})(?:[/?#]|$)", url)
    return match.group(1) if match else ""


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _numeric(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    match = re.search(r"(?<!\d)(\d[\d.,]*)(?!\d)", raw)
    if not match:
        return None
    text = match.group(1)
    if "," in text and "." in text:
        # Chilean prices normally use dots for thousands and a comma for decimals.
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
        text = text.replace(".", "")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _price_value(extracted: dict[str, Any]) -> tuple[Decimal | None, str]:
    currency = str(extracted.get("price_currency") or "").strip().upper()
    if currency == "CLF":
        currency = "UF"
    value = _numeric(extracted.get("price_value"))
    if value is None:
        raw = str(extracted.get("price") or "")
        value = _numeric(raw)
        if not currency:
            if re.search(r"\b(?:UF|CLF)\b", raw, re.I):
                currency = "UF"
            elif "$" in raw or "CLP" in raw.upper():
                currency = "CLP"
    if currency not in {"UF", "CLP"}:
        currency = ""
    return value, currency


def _price_in_uf(raw: str) -> float | None:
    """Compatibility parser: return a price only when the source labels it UF."""
    value, currency = _price_value({"price": raw})
    return float(value) if value is not None and currency == "UF" else None


def classify_with_rules(fields: dict[str, Any]) -> dict[str, Any] | None:
    """Compatibility name for the deterministic Yapo classifier."""
    return classify_yapo_listing(fields)


def _scope_url_is_valid(url: str) -> tuple[bool, str]:
    """Compatibility validator backed by the general URL parser, without a fixed commune scope."""
    try:
        search = search_config_from_url(url)
    except SearchConfigurationError as exc:
        return False, str(exc)
    expected = {item[0] for item in search.start_urls()}
    parsed = urlparse(url)
    if parsed.path.rstrip("/") != urlparse(next(iter(expected))).path.rstrip("/"):
        return False, "requested_route_does_not_match_parsed_search"
    return True, "configured_yapo_search_route_and_filters"


def _normalize_location_piece(value: str) -> str:
    try:
        from comuna_utils import normalize_commune_slug
    except ImportError:  # direct script execution
        from ..comuna_utils import normalize_commune_slug  # type: ignore
    return normalize_commune_slug(value) or ""


def _commune_matches(value: str, target: str, region_slug: str) -> bool:
    target_slug = _normalize_location_piece(target)
    if not target_slug:
        return False
    region_names = {region_slug, region_slug.removeprefix("region-")}
    region_names.update({f"{name}-de-santiago" for name in list(region_names) if name == "metropolitana"})
    region_names = {_normalize_location_piece(name) for name in region_names if name}
    text = str(value or "").strip()
    if not text:
        return False
    pieces = [_normalize_location_piece(piece) for piece in re.split(r"[,|/]", text)]
    pieces = [piece for piece in pieces if piece]
    commune_pieces = [piece for piece in pieces if piece not in region_names and not piece.startswith("region-")]
    # A comma-separated address may include municipality, region and a second
    # commune. Accept it only when exactly one commune-like component remains.
    if len(commune_pieces) == 1 and commune_pieces[0] == target_slug:
        return True
    if len(commune_pieces) > 1:
        return False
    whole = _normalize_location_piece(text)
    return whole == target_slug or any(
        whole == f"{target_slug}-{region}" or whole == f"{region}-{target_slug}"
        for region in region_names
    )


def _commune_validation(
    detail_value: str,
    card_value: str,
    target: str,
    region_slug: str,
    detail_status: str = "",
) -> tuple[str, str]:
    detail = str(detail_value or "").strip()
    card = str(card_value or "").strip()
    if detail_status == "pending_ambiguous":
        return "pending_conflicting_location_evidence", "detail_extractor_found_conflicting_locations"
    if detail and detail_status and detail_status not in {"confirmed_detail", "confirmed"}:
        return "pending", "detail_commune_evidence_not_confirmed"
    # A matching search card is useful provenance, but cannot replace the
    # property detail's location when deciding whether the individual listing
    # passes the requested commune filter.
    if not detail:
        if card and not _commune_matches(card, target, region_slug):
            normalized_card = _normalize_location_piece(card)
            if normalized_card and not any(char in card for char in ",|/"):
                return "confirmed_other_commune", "card_commune_is_different"
        return "pending", "detail_commune_not_confirmed"
    if detail and _commune_matches(detail, target, region_slug):
        if card and not _commune_matches(card, target, region_slug):
            return "pending_conflicting_location_evidence", "detail_and_card_commune_disagree"
        return "confirmed_detail", "exact_commune_in_detail"
    if detail:
        normalized = _normalize_location_piece(detail)
        if any(char in detail for char in ",|/"):
            pieces = [_normalize_location_piece(part) for part in re.split(r"[,|/]", detail)]
            commune_pieces = [piece for piece in pieces if piece and not piece.startswith("region-")]
            if len(commune_pieces) == 1:
                return "confirmed_other_commune", "detail_commune_is_different"
            return "pending_conflicting_location_evidence", "detail_commune_contains_multiple_location_components"
        if normalized and not any(char in detail for char in ",|/"):
            return "confirmed_other_commune", "detail_commune_is_different"
    if card:
        normalized = _normalize_location_piece(card)
        if normalized and not any(char in card for char in ",|/"):
            return "confirmed_other_commune", "card_commune_is_different"
    return "pending", "commune_not_explicitly_confirmed"


def _detail_region_value(extracted: dict[str, Any]) -> str:
    region = str(extracted.get("region") or extracted.get("region_original") or "").strip()
    if region:
        return region
    evidence = extracted.get("comuna_evidence")
    header = str(evidence.get("header_location") or "").strip() if isinstance(evidence, dict) else ""
    pieces = [piece.strip() for piece in header.split(",") if piece.strip()]
    if len(pieces) >= 2 and ("region" in _normalize_location_piece(pieces[0]) or _normalize_location_piece(pieces[0]) in {"rm", "metropolitana"}):
        return pieces[0]
    return ""


def _region_validation(extracted: dict[str, Any], search: SearchConfig) -> tuple[str, str, str]:
    observed = _detail_region_value(extracted)
    if not observed:
        return "confirmed_by_search_scope", "verified_search_route_and_exact_commune", ""
    try:
        observed_slug = normalize_region_slug(observed)
    except SearchConfigurationError:
        return "pending", "detail_region_value_not_normalizable", observed
    if observed_slug == search.region_slug:
        return "confirmed_detail", "exact_region_in_detail_header", observed
    return "confirmed_other_region", "detail_region_is_different", observed


def _lookup_numeric(extracted: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        value = _numeric(extracted.get(key))
        if value is not None:
            return value
    attrs = extracted.get("attributes")
    if isinstance(attrs, dict):
        normalized = {re.sub(r"[^a-z0-9]+", "", str(key).lower()): value for key, value in attrs.items()}
        for key in keys:
            probe = re.sub(r"[^a-z0-9]+", "", key.lower())
            value = _numeric(normalized.get(probe))
            if value is not None:
                return value
    return None


def _fresh_uf_rate(rate: Any, rate_date: str, *, today: date | None = None) -> bool:
    value = _numeric(rate)
    if value is None or value <= 0:
        return False
    try:
        parsed = date.fromisoformat(str(rate_date))
    except (TypeError, ValueError):
        return False
    current = today or datetime.now(timezone.utc).date()
    age_days = (current - parsed).days
    return 0 <= age_days <= 7


def _within_range(value: Decimal, minimum: Decimal | None, maximum: Decimal | None) -> bool:
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)


def validate_listing_filters(
    search: SearchConfig,
    listing: dict[str, Any],
    extracted: dict[str, Any],
    *,
    uf_value_clp: Any = None,
    uf_date: str = "",
    today: date | None = None,
) -> dict[str, Any]:
    """Recheck every requested filter against detail/card evidence, fail closed."""
    reasons: list[str] = []
    checks: dict[str, dict[str, Any]] = {}
    url_path = urlparse(str(listing.get("url") or "")).path.lower()
    category_prefix = f"/{search.category_slug}/"
    if url_path.startswith(category_prefix):
        checks["operation_and_category"] = {"status": "pasa", "source": "yapo_detail_category_path"}
    else:
        checks["operation_and_category"] = {
            "status": "fuera_de_filtro", "observed_path": url_path,
            "expected_category": search.category_slug,
        }
        reasons.append("detail_category_or_operation_mismatch")

    commune_status, commune_reason = _commune_validation(
        str(extracted.get("comuna") or ""),
        str(listing.get("discovery_comuna") or ""),
        str(listing.get("search_commune") or ""),
        str(listing.get("region_slug") or ""),
        str((extracted.get("comuna_evidence") or {}).get("status") or ""),
    )
    if commune_status in {"confirmed_detail", "confirmed_card"}:
        checks["commune"] = {"status": "pasa", "evidence": commune_reason,
                              "detail": extracted.get("comuna"), "card": listing.get("discovery_comuna")}
    elif commune_status == "confirmed_other_commune":
        checks["commune"] = {"status": "fuera_de_filtro", "evidence": commune_reason,
                              "detail": extracted.get("comuna"), "card": listing.get("discovery_comuna")}
        reasons.append("commune_mismatch")
    else:
        checks["commune"] = {"status": "pendiente", "evidence": commune_reason,
                              "detail": extracted.get("comuna"), "card": listing.get("discovery_comuna")}
        reasons.append("commune_not_confirmed")

    region_status, region_reason, region_value = _region_validation(extracted, search)
    if region_status in {"confirmed_detail", "confirmed_by_search_scope"}:
        checks["region"] = {
            "status": "pasa", "evidence": region_reason,
            "observed": region_value or None, "search_scope": search.region_slug,
        }
    elif region_status == "confirmed_other_region":
        checks["region"] = {
            "status": "fuera_de_filtro", "evidence": region_reason,
            "observed": region_value, "expected": search.region_slug,
        }
        reasons.append("region_mismatch")
    else:
        checks["region"] = {
            "status": "pendiente", "evidence": region_reason,
            "observed": region_value or None, "expected": search.region_slug,
        }
        reasons.append("region_not_confirmed")

    # Yapo's shared lots category contains both terrains and parcels. Keep the
    # narrower request pending unless the listing itself names the requested type.
    if search.property_type in {"terreno", "parcela"}:
        type_text = " ".join(str(extracted.get(key) or "") for key in ("title", "description"))
        attrs = extracted.get("attributes") if isinstance(extracted.get("attributes"), dict) else {}
        type_text += " " + " ".join(str(value) for value in attrs.values())
        normalized_type_text = _normalize_location_piece(type_text)
        if search.property_type == "parcela":
            is_parcel = bool(re.search(r"\bparcelas?\b", normalized_type_text.replace("-", " ")))
            is_explicit_other = bool(re.search(r"\b(?:sitio|terreno urbano)\b", normalized_type_text.replace("-", " ")))
            if is_parcel:
                checks["property_subtype"] = {"status": "pasa", "evidence": "explicit_parcela_text"}
            elif is_explicit_other:
                checks["property_subtype"] = {"status": "fuera_de_filtro", "evidence": "explicit_non_parcela_text"}
                reasons.append("property_subtype_mismatch")
            else:
                checks["property_subtype"] = {"status": "pendiente", "evidence": "shared_lots_category_needs_detail_subtype"}
                reasons.append("property_subtype_not_confirmed")
        else:
            checks["property_subtype"] = {"status": "pasa", "evidence": "yapo_lots_and_terrains_category"}

    price_value, source_currency = _price_value(extracted)
    requested_currency = search.price_currency
    price_min = search.price_min
    price_max = search.price_max
    if search.price_min is not None or search.price_max is not None or requested_currency:
        normalized_price = price_value
        conversion = "native"
        if price_value is None or not source_currency:
            checks["price"] = {"status": "pendiente", "evidence": "detail_price_or_currency_missing",
                               "price_raw": extracted.get("price")}
            reasons.append("price_not_confirmed")
        elif requested_currency and source_currency != requested_currency:
            if not _fresh_uf_rate(uf_value_clp, uf_date, today=today):
                checks["price"] = {
                    "status": "pendiente", "evidence": "uf_conversion_rate_missing_or_stale",
                    "price_value": str(price_value), "price_currency": source_currency,
                    "uf_value_clp": str(uf_value_clp or ""), "uf_date": uf_date,
                }
                reasons.append("price_conversion_unverified")
                normalized_price = None
            else:
                rate = _numeric(uf_value_clp)
                assert rate is not None
                normalized_price = price_value / rate if requested_currency == "UF" else price_value * rate
                conversion = f"{source_currency}_to_{requested_currency}_using_uf_{rate}_on_{uf_date}"
        if normalized_price is not None:
            range_pass = _within_range(normalized_price, price_min, price_max)
            if not range_pass:
                checks["price"] = {
                    "status": "fuera_de_filtro", "value_in_requested_currency": str(normalized_price),
                    "requested_currency": requested_currency or source_currency,
                    "minimum": str(price_min) if price_min is not None else None,
                    "maximum": str(price_max) if price_max is not None else None,
                    "conversion": conversion,
                }
                reasons.append("price_out_of_range")
            else:
                checks["price"] = {
                    "status": "pasa", "value_in_requested_currency": str(normalized_price),
                    "requested_currency": requested_currency or source_currency,
                    "conversion": conversion,
                }

    for name, minimum, maximum, keys in (
        ("bedrooms", search.bedrooms_min, search.bedrooms_max, ("dormitorios", "rooms", "bedrooms")),
        ("bathrooms", search.bathrooms_min, search.bathrooms_max, ("banos", "baños", "bathrooms")),
        ("surface_m2", search.surface_min_m2, search.surface_max_m2,
         ("superficie_total", "m2_totales", "superficie_util", "m2_construidos", "m2")),
    ):
        if minimum is None and maximum is None:
            continue
        observed = _lookup_numeric(extracted, keys)
        if observed is None:
            checks[name] = {"status": "pendiente", "evidence": "requested_detail_value_missing"}
            reasons.append(f"{name}_not_confirmed")
        elif _within_range(observed, minimum, maximum):
            checks[name] = {"status": "pasa", "observed": str(observed), "source_fields": list(keys)}
        else:
            checks[name] = {
                "status": "fuera_de_filtro", "observed": str(observed),
                "minimum": str(minimum) if minimum is not None else None,
                "maximum": str(maximum) if maximum is not None else None,
            }
            reasons.append(f"{name}_out_of_range")

    statuses = {item["status"] for item in checks.values()}
    if "fuera_de_filtro" in statuses:
        result = "FUERA_DE_FILTRO"
    elif "pendiente" in statuses:
        result = "PENDIENTE_VALIDACION"
    else:
        result = "PASA"
    return {"status": result, "checks": checks, "reasons": reasons}


def _extraction_issues(extracted: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    description = str(extracted.get("description") or extracted.get("descripcion") or "")
    if not extracted.get("title"):
        issues.append("title_missing")
    if not description and extracted.get("description_summary"):
        issues.append("full_description_missing;meta_summary_separate")
    elif not description:
        issues.append("full_description_missing")
    if extracted.get("descripcion_is_truncated"):
        issues.append("description_marked_truncated")
    if not extracted.get("price"):
        issues.append("price_missing")
    commune_evidence = extracted.get("comuna_evidence") if isinstance(extracted.get("comuna_evidence"), dict) else {}
    if not extracted.get("comuna") or commune_evidence.get("status") != "confirmed_detail":
        issues.append("detail_commune_not_confirmed")
    return issues


def _apply_schema_price_fields(extracted: dict[str, Any], config: AppConfig) -> None:
    """Keep native price values and derive the other currency only from a fresh UF rate."""
    amount, currency = _price_value(extracted)
    if amount is None or currency not in {"UF", "CLP"}:
        extracted.setdefault("precio_raw", str(extracted.get("price") or ""))
        extracted.setdefault("precio_moneda_original", "UNKNOWN")
        extracted.setdefault("precio_original_num", None)
        extracted.setdefault("precio_uf", None)
        extracted.setdefault("precio_clp", None)
        extracted["precio_conversion_source"] = "native_currency_unconfirmed"
        return

    number = float(amount)
    fresh = _fresh_uf_rate(config.uf_valor_clp, config.uf_fecha)
    rate = _numeric(config.uf_valor_clp) if fresh else None
    extracted["precio_raw"] = str(extracted.get("price") or "")
    extracted["precio_moneda_original"] = currency
    extracted["precio_original_num"] = number
    extracted["precio_uf"] = number if currency == "UF" else (round(number / float(rate), 2) if rate else None)
    extracted["precio_clp"] = int(round(number * float(rate))) if currency == "UF" and rate else (
        int(number) if currency == "CLP" else None
    )
    extracted["precio_uf_normalizado"] = extracted["precio_uf"]
    extracted["precio_clp_normalizado"] = extracted["precio_clp"]
    extracted["uf_valor_usado"] = float(rate) if rate else None
    extracted["uf_fecha"] = str(config.uf_fecha) if rate else ""
    extracted["uf_source"] = str(getattr(config, "uf_source", "") or "") if rate else ""
    extracted["precio_conversion_source"] = "documented_fresh_uf_rate" if rate else "native_currency_only"
    extracted["precio_conversion_fuente"] = str(getattr(config, "uf_source", "") or "") if rate else ""


def _page_validator(search: SearchConfig):
    def validate_page(url: str, html: str) -> dict[str, Any]:
        path = urlparse(url).path.rstrip("/")
        for commune in search.communes:
            if search_path_matches_config(path, search, commune):
                result = verify_search_page(url, html, search, commune)
                result["commune"] = commune
                return result
        return {"verified": False, "reason": "pagination_left_configured_search_route"}
    return validate_page


def _detail_property_type(search: SearchConfig, record: dict[str, Any], extracted: dict[str, Any]) -> str:
    path = urlparse(str(record.get("url") or "")).path.lower()
    for property_type, operations in PROPERTY_CATEGORIES.items():
        if operations.get(search.operation) and path.startswith(f"/{operations[search.operation]}/"):
            return property_type
    # Parcels and general terrain listings share Yapo's category. The subtype
    # filter below uses detail text and retains uncertainty instead of guessing.
    return ""


def _save_search_outputs(summary: dict[str, Any], records: list[dict[str, Any]], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    batch = str(summary["batch_id"])
    summary_path = output_dir / f"yapo_search_{batch}_summary.json"
    records_path = output_dir / f"yapo_search_{batch}_records.json"
    csv_path = output_dir / f"yapo_search_{batch}_records.csv"
    fields = (
        "schema_version", "origen", "source_portal", "source", "run_id", "batch_id",
        "listing_id", "url", "canonical_url", "source_search_url", "page_number",
        "region", "region_original", "region_search_scope", "comuna", "comuna_original",
        "comuna_slug", "operation", "operacion", "property_type", "tipo_propiedad",
        "price_raw", "precio_raw", "price_value", "price_currency", "precio_moneda_original",
        "precio_original_num", "precio_uf", "precio_clp", "uf_value_clp", "uf_date", "uf_source",
        "filter_status", "filter_validation",
        "title", "description", "publicador_visible", "publicador", "seller_name",
        "contact_name", "contact_phone", "contact_email", "image_urls", "enlaces_fotos",
        "dormitorios", "banos", "estacionamientos", "m2_construidos", "m2_totales",
        "superficie_total", "superficie_util", "direccion_exacta", "attributes", "raw_attributes",
        "details", "publisher_profile_context", "publisher_field_provenance",
        "publisher_identity_candidates", "seller_type", "seller_profile_id", "seller_profile_url",
        "listing_advertiser", "listing_advertiser_type", "classification", "classification_evidence",
        "classification_debug", "gestion", "pipeline_state",
        "pipeline_complete", "scrape_stage", "processed_at", "updated_at", "fecha_scraping",
        "extraction_issues",
    )
    _write_json(records_path, records)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row = dict(record)
            for field in (
                "filter_validation", "classification", "classification_evidence", "extraction_issues",
                "gestion", "image_urls", "enlaces_fotos", "attributes", "raw_attributes", "details",
                "publisher_profile_context", "publisher_field_provenance", "publisher_identity_candidates",
                "classification_debug",
            ):
                row[field] = json.dumps(record.get(field), ensure_ascii=False, separators=(",", ":"))
            writer.writerow(row)
    summary["records_json"] = str(records_path)
    summary["records_csv"] = str(csv_path)
    summary["summary_report"] = str(summary_path)
    _write_json(summary_path, summary)
    return {"summary": str(summary_path), "records_json": str(records_path), "records_csv": str(csv_path)}


def _apply_yapo_availability_hold(record: dict[str, Any]) -> None:
    """Require a current explicit availability signal before assignment can be ready."""
    status = str(record.get("availability_status") or "NO_VERIFICADO").upper()
    record.setdefault("availability_status", status)
    record.setdefault("availability_checked_at", "")
    record.setdefault("availability_source", "not_observed")
    record.setdefault("availability_evidence", ["no_current_availability_check"])
    if status == "ACTIVO":
        return
    classification = dict(record.get("classification") or {})
    reason = (
        "yapo_listing_deactivated"
        if status == "DESACTIVADO"
        else "yapo_availability_not_confirmed"
    )
    reasons = list(classification.get("assignment_block_reasons") or [])
    classification.update({
        "manual_review_required": True,
        "manual_review_reason": reason,
        "assignment_ready": False,
        "exclude_from_assignment": True,
        "assignment_block_reasons": sorted(set([*reasons, reason])),
    })
    record["classification"] = classification
    record["assignment_ready"] = False
    record["exclude_from_assignment"] = True
    record["assignment_block_reasons"] = classification["assignment_block_reasons"]


def _reclassify_batch_by_exact_profile(records_out: list[dict[str, Any]]) -> None:
    """Apply exact-profile evidence after every listing in the search batch is parsed."""
    indexes: list[int] = []
    classifier_inputs: list[dict[str, Any]] = []
    passthrough_fields = (
        "listing_id", "url", "seller_profile_id", "seller_profile_url", "seller_type",
        "listing_advertiser", "listing_advertiser_type", "listing_advertiser_type_source",
        "publicador_visible", "publicador", "seller_name", "contact_name", "contact_logo_alt",
        "seller_jsonld_name", "contact_badges_text", "publisher_field_provenance",
        "publisher_profile_context", "title", "description", "descripcion", "images", "image_urls",
    )
    for index, record in enumerate(records_out):
        details = record.get("details")
        if not isinstance(details, dict) or str(record.get("html_validation_status") or "").upper() != "OK":
            _apply_yapo_availability_hold(record)
            continue
        extracted = dict(details)
        for field in passthrough_fields:
            value = record.get(field)
            if value not in (None, "", [], {}):
                extracted[field] = value
        indexes.append(index)
        classifier_inputs.append(extracted)

    batch_results = classify_yapo_batch(classifier_inputs) if classifier_inputs else []
    for index, result in zip(indexes, batch_results):
        record = records_out[index]
        if isinstance(result, dict):
            record["classification"] = result
            record["classification_evidence"] = result.get("evidence") or []
            record["classification_debug"] = {
                "evidence_strength": result.get("evidence_strength"),
                "contradictions": result.get("contradictions") or [],
                "requires_review": result.get("requires_review"),
                "profile_corroboration": result.get("profile_corroboration"),
                "deepseek_enabled": False,
                "deepseek_call_executed": False,
            }
            record["pipeline_complete"] = bool(
                not record.get("extraction_issues")
                and result.get("state") in {
                    "CORREDOR_SEGURO", "CORREDOR_PROBABLE", "DUEÑO_SEGURO", "DUEÑO_PROBABLE",
                }
            )
            record["pipeline_state"] = "CLASSIFIED" if record["pipeline_complete"] else "UNCERTAIN_PENDING_REVIEW"
        _apply_yapo_availability_hold(record)


def run_search(search: SearchConfig, config: AppConfig | None = None) -> dict[str, Any]:
    """Run the shared read-only search engine; offline simulation is the default."""
    if not isinstance(search, SearchConfig):
        raise TypeError("run_search requiere SearchConfig validado.")
    config = config or AppConfig()
    config.ensure_layout()
    batch_id = search.batch_id or config.generate_batch_id()
    timestamp = datetime.now(timezone.utc).isoformat()
    scopes = search.start_urls()
    output_dir = Path(search.output_dir).expanduser() if search.output_dir else config.reports_dir / "general_search"
    summary: dict[str, Any] = {
        "engine": "scrapers.scraper_yapo.pilot.run_search",
        "generated_at": timestamp,
        "scope": search.to_dict(),
        "batch_id": batch_id,
        "start_urls": [{"url": url, "commune": commune} for url, commune in scopes],
        "mode": "live_read_only" if search.live else "offline_simulation",
        "pages_visited": 0, "pagination_complete": False,
        "pagination_cause": "offline_simulation_no_requests" if not search.live else "not_started",
        "listing_selection_complete": True, "selection_truncated": False,
        "selection_dropped_count": 0,
        "search_filters_verified": False, "page_log": [], "errors_or_blocks": [],
        "ads_discovered": 0, "batch_duplicates": 0, "mongo_existing": 0,
        "new_ads": 0, "mongo_dedup_verified": False,
        "mongo_dedup_status": "not_attempted", "filter_matches": 0,
        "excluded_out_of_filter": 0, "pending_filter_validation": 0,
        "commercial_candidate_count": 0, "broker_excluded_count": 0,
        "preliminary_classification_counts": {}, "extraction_error_counts": {},
        "proxy_requests": 0, "request_attempts": 0, "successful_downloads": 0,
        "cache_responses": 0, "invalid_or_removed_ads": 0,
        "deepseek_calls": 0, "deepseek_enabled": False, "mongo_writes": 0,
        "assignment_runs": 0, "distribution_runs": 0, "notifications": 0,
        "uf_value_clp": config.uf_valor_clp, "uf_date": config.uf_fecha,
        "uf_source": str(getattr(config, "uf_source", "") or ""),
        "uf_conversion_fresh": _fresh_uf_rate(config.uf_valor_clp, config.uf_fecha),
        "filter_evidence": FILTER_EVIDENCE,
        "all_region_communes_supported": False,
        "commercial_result_available": False,
    }
    records_out: list[dict[str, Any]] = []
    if not search.live:
        summary["pagination_cause"] = "offline_simulation_no_requests"
        summary["files"] = _save_search_outputs(summary, records_out, output_dir)
        return summary

    proxy_manager = ProxyManager.from_config(config)
    if not proxy_manager.has_proxies():
        summary["pagination_cause"] = "proxy_unavailable"
        summary["errors_or_blocks"].append("FlashProxy no está configurado; se bloqueó cualquier conexión directa.")
        summary["files"] = _save_search_outputs(summary, records_out, output_dir)
        return summary

    discovery_report: dict[str, Any] = {}
    try:
        discovered = discover_listing_urls(
            [url for url, _ in scopes],
            max_pages=search.max_pages,
            max_urls=search.max_listings,
            until_end=True,
            batch_id=batch_id,
            target_communes=list(search.communes),
            report=discovery_report,
            config=config,
            proxy_manager=proxy_manager,
            page_filter_validator=_page_validator(search),
        )
    except Exception as exc:
        discovered = []
        discovery_report.update({
            "pages": [], "pagination_complete": False,
            "pagination_cause": "discovery_exception",
            "errors": [f"{type(exc).__name__}: {exc}"],
        })

    pages = discovery_report.get("pages") or []
    summary.update({
        "pages_visited": len(pages),
        "pagination_complete": bool(discovery_report.get("pagination_complete")),
        "pagination_cause": discovery_report.get("pagination_cause") or "unknown",
        "listing_selection_complete": not bool(discovery_report.get("selection_truncated")),
        "selection_truncated": bool(discovery_report.get("selection_truncated")),
        "selection_dropped_count": int(discovery_report.get("selection_dropped_count") or 0),
        "page_log": pages,
        "errors_or_blocks": list(discovery_report.get("errors") or []),
        "proxy_requests": int(discovery_report.get("proxy_requests") or 0),
        "request_attempts": int(discovery_report.get("request_attempts") or 0),
        "successful_downloads": int(discovery_report.get("successful_downloads") or 0),
        "cache_responses": int(discovery_report.get("cache_responses") or 0),
    })
    summary["search_filters_verified"] = bool(pages) and all(
        bool((page.get("filter_validation") or {}).get("verified"))
        for page in pages
    )
    parsed_pages = [page for page in pages if page.get("status") in {"complete", "empty"}]
    summary["pages_complete"] = len(parsed_pages)
    summary["pages_not_verified"] = len(pages) - len(parsed_pages)
    summary["ads_discovered"] = sum(int(page.get("listing_count") or 0) for page in parsed_pages)
    summary["batch_duplicates"] = sum(int(page.get("duplicate_count") or 0) for page in parsed_pages)

    # Repeated commune searches can return the same ad. Deduplicate in memory
    # before the read-only Mongo identity lookup.
    unique: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for row in discovered:
        normalized_url = normalize_yapo_url(str(row.get("url") or ""))
        listing_id = str(row.get("listing_id") or _listing_id(normalized_url))
        if not normalized_url or not listing_id:
            summary["errors_or_blocks"].append("discovered_listing_missing_identity")
            continue
        if normalized_url in seen_urls or listing_id in seen_ids:
            summary["batch_duplicates"] += 1
            continue
        seen_urls.add(normalized_url)
        seen_ids.add(listing_id)
        source_url = str(row.get("source_search_url") or "")
        commune = next((slug for url, slug in scopes if url == source_url), "")
        unique.append({
            **row, "url": normalized_url, "listing_id": listing_id,
            "source_search_url": source_url,
            "search_commune": commune,
            "region_slug": str(getattr(search, "region_slug")),
        })

    summary["ads_discovered_unique"] = len(unique)
    if not unique:
        summary["mongo_dedup_verified"] = True
        summary["mongo_dedup_status"] = "not_required_no_ads"
        new_records: list[dict[str, Any]] = []
    elif not config.mongo_uri:
        summary["mongo_dedup_verified"] = False
        summary["mongo_dedup_status"] = "unverified_missing_mongo_uri"
        summary["errors_or_blocks"].append("MONGO_URI ausente; no se pueden marcar anuncios como nuevos.")
        new_records = []
    else:
        try:
            store = MongoStore(config)
            ids = [row["listing_id"] for row in unique]
            urls = [str(value) for row in unique for value in (row.get("url"), row.get("canonical_url")) if value]
            existing_ids = store.existing_listing_ids(ids)
            existing_urls = store.existing_listing_urls(urls)
            summary["mongo_dedup_verified"] = True
            summary["mongo_dedup_status"] = "verified_read_only"
            summary["mongo_existing"] = sum(
                row["listing_id"] in existing_ids or normalize_yapo_url(row["url"]) in existing_urls
                for row in unique
            )
            new_records = [
                row for row in unique
                if row["listing_id"] not in existing_ids
                and normalize_yapo_url(row["url"]) not in existing_urls
            ]
        except Exception as exc:
            summary["mongo_dedup_verified"] = False
            summary["mongo_dedup_status"] = "unverified_unavailable"
            summary["errors_or_blocks"].append(f"mongo_dedup_unverified:{type(exc).__name__}")
            new_records = []
    summary["new_ads"] = len(new_records) if summary["mongo_dedup_verified"] else None

    if summary["search_filters_verified"] and summary["mongo_dedup_verified"]:
        for row in new_records:
            summary["request_attempts"] += int(not html_path_for_url(row["url"], config, batch_id=batch_id).exists())
            try:
                downloaded = download_html(row["url"], config, proxy_manager, batch_id=batch_id)
                if downloaded.fetch_source == "cache":
                    summary["cache_responses"] += 1
                elif downloaded.fetch_source == "requests_proxy":
                    summary["successful_downloads"] += 1
                    summary["proxy_requests"] += 1
                validation = validate_html(downloaded.html)
                availability = assess_yapo_availability(
                    downloaded.html,
                    fetch_source=str(getattr(downloaded, "fetch_source", "unknown") or "unknown"),
                    validation_status=str(validation.get("status") or ""),
                    status_code=getattr(downloaded, "status_code", None),
                    flashproxy_verified=str(getattr(downloaded, "proxy_provider", "")) == "flashproxy",
                )
                if downloaded.blocked or validation["status"] == "BLOCKED":
                    summary["errors_or_blocks"].append(f"listing_blocked:{row['listing_id']}")
                    records_out.append({
                        **row, "filter_status": "PENDIENTE_VALIDACION",
                        "filter_validation": {"status": "PENDIENTE_VALIDACION", "reasons": ["detail_html_blocked"]},
                        "classification": {}, "classification_evidence": [],
                        "extraction_issues": ["listing_blocked"], "detail_error": "blocked",
                        **availability,
                    })
                    continue
                if validation["status"] in {"INVALID", "LISTING_REMOVED"}:
                    summary["invalid_or_removed_ads"] += 1
                    summary["errors_or_blocks"].append(f"listing_invalid_or_removed:{row['listing_id']}:{validation['status']}")
                    records_out.append({
                        **row, "filter_status": "PENDIENTE_VALIDACION",
                        "filter_validation": {"status": "PENDIENTE_VALIDACION", "reasons": [validation["status"]]},
                        "classification": {}, "classification_evidence": [],
                        "extraction_issues": [f"html_{validation['status'].lower()}"],
                        **availability,
                    })
                    continue
                extracted = extract_listing_fields(downloaded.html, source_url=row["url"])
                detail_commune_source = str(extracted.get("comuna") or "")
                detail_commune_evidence = extracted.get("comuna_evidence") if isinstance(extracted.get("comuna_evidence"), dict) else {}
                detail_commune_provenance = str(extracted.get("comuna_source") or "")
                extracted["discovery_comuna"] = row.get("discovery_comuna", "")
                extracted = _enrich_property_fields(extracted, row["url"], config.uf_valor_clp, config.uf_fecha)
                # The shared enrichment adapter historically fills a missing
                # detail commune from the search card. Preserve the detail
                # extractor's own value and provenance so a card-only location
                # cannot become confirmed identity.
                extracted["comuna"] = detail_commune_source
                extracted["comuna_source"] = detail_commune_provenance
                extracted["comuna_evidence"] = detail_commune_evidence
                _apply_schema_price_fields(extracted, config)
                filter_validation = validate_listing_filters(
                    search, row, extracted,
                    uf_value_clp=config.uf_valor_clp,
                    uf_date=config.uf_fecha,
                )
                state_from_path = _detail_property_type(search, row, extracted)
                classification = classify_with_rules(extracted) or {
                    "state": "INCIERTO", "reason": "Clasificación determinista no disponible.",
                    "evidence": [], "source": "technical_validation",
                }
                evidence = classification.get("evidence") or []
                detail_evidence = extracted.get("comuna_evidence") if isinstance(extracted.get("comuna_evidence"), dict) else {}
                detail_commune = str(extracted.get("comuna") or "") if detail_evidence.get("status") == "confirmed_detail" else ""
                detail_region = _detail_region_value(extracted)
                description = str(extracted.get("description") or extracted.get("descripcion") or "")
                images = list(extracted.get("images") or extracted.get("image_urls") or [])
                schema_property_type = state_from_path or str(extracted.get("tipo_propiedad") or "")
                if search.property_type in {"terreno", "parcela"}:
                    subtype = (filter_validation.get("checks") or {}).get("property_subtype", {}).get("status")
                    schema_property_type = search.property_type if subtype == "pasa" else ""
                extraction_issues = _extraction_issues(extracted)
                classification_state = str(classification.get("state") or "")
                schema_pipeline_complete = (
                    not extraction_issues
                    and classification_state in {"CORREDOR_SEGURO", "CORREDOR_PROBABLE", "DUEÑO_SEGURO", "DUEÑO_PROBABLE"}
                )
                pipeline_state = "CLASSIFIED" if schema_pipeline_complete else (
                    "UNCERTAIN_PENDING_REVIEW" if classification_state == "INCIERTO" else "EXTRACTOR_DEGRADED"
                )
                result = {
                    **row,
                    "schema_version": "yapo_propiedades_v1",
                    "origen": "yapo",
                    "source_portal": "yapo",
                    "source": "owner_hunt",
                    "run_id": batch_id,
                    "batch_id": batch_id,
                    "canonical_url": row["url"],
                    "region": detail_region,
                    "region_original": detail_region,
                    "region_search_scope": search.region,
                    "comuna": detail_commune,
                    "comuna_original": detail_commune,
                    "comuna_slug": _normalize_location_piece(detail_commune) if detail_commune else "",
                    "operation": search.operation,
                    "operacion": search.operation,
                    "property_type": search.property_type,
                    "tipo_propiedad": schema_property_type,
                    "detail_property_type_from_category": state_from_path,
                    "price_raw": str(extracted.get("price") or ""),
                    "price": str(extracted.get("price") or ""),
                    "precio_raw": str(extracted.get("price") or ""),
                    "price_value": str(extracted.get("price_value") or ""),
                    "price_currency": extracted.get("price_currency") or "",
                    "precio_moneda_original": extracted.get("precio_moneda_original") or "UNKNOWN",
                    "precio_original_num": extracted.get("precio_original_num"),
                    "precio_uf": extracted.get("precio_uf"),
                    "precio_clp": extracted.get("precio_clp"),
                    "precio_uf_normalizado": extracted.get("precio_uf_normalizado"),
                    "precio_clp_normalizado": extracted.get("precio_clp_normalizado"),
                    "uf_valor_usado": extracted.get("uf_valor_usado"),
                    "uf_fecha": extracted.get("uf_fecha") or "",
                    "uf_value_clp": extracted.get("uf_valor_usado"),
                    "uf_date": extracted.get("uf_fecha") or "",
                    "uf_source": extracted.get("uf_source") or "",
                    "precio_conversion_source": extracted.get("precio_conversion_source") or "",
                    "precio_conversion_fuente": extracted.get("precio_conversion_fuente") or "",
                    "filter_status": filter_validation["status"],
                    "filter_validation": filter_validation,
                    "title": extracted.get("title") or "",
                    # Preserve the full description returned by the extractor.
                    "description": description,
                    "descripcion": description,
                    "body_text": description,
                    "body_text_excerpt": description[:600],
                    "body_text_len": len(description),
                    "descripcion_len": len(description),
                    "descripcion_is_truncated": bool(extracted.get("descripcion_is_truncated")),
                    "descripcion_source": extracted.get("description_source") or "",
                    "description_available": bool(description.strip()),
                    "publicador_visible": extracted.get("publicador_visible") or "",
                    "publicador": extracted.get("publicador_visible") or extracted.get("contact_name") or "",
                    "seller_name": extracted.get("seller_name") or extracted.get("publicador_visible") or "",
                    "seller_type": extracted.get("seller_type") or "",
                    "seller_profile_id": extracted.get("seller_profile_id") or "",
                    "seller_profile_url": extracted.get("seller_profile_url") or "",
                    "seller_jsonld_name": extracted.get("seller_jsonld_name") or "",
                    "listing_advertiser": extracted.get("listing_advertiser") or "",
                    "listing_advertiser_type": extracted.get("listing_advertiser_type") or "",
                    "contact_logo_alt": extracted.get("contact_logo_alt") or "",
                    "contact_badges_text": extracted.get("contact_badges_text") or "",
                    "seller_text": extracted.get("seller_text") or "",
                    "seller_avatar_alt": extracted.get("seller_avatar_alt") or "",
                    "contact_name": extracted.get("contact_name") or "",
                    "contact_phone": extracted.get("contact_phone") or "",
                    "contact_email": extracted.get("contact_email") or "",
                    "images": images,
                    "image_urls": images,
                    "enlaces_fotos": images,
                    "image_urls_count": len(images),
                    "main_image_url": str(extracted.get("main_image_url") or next(iter(images), "")),
                    "attributes": extracted.get("attributes") or {},
                    "raw_attributes": extracted.get("attributes") or {},
                    "dormitorios": extracted.get("dormitorios"),
                    "banos": extracted.get("banos"),
                    "estacionamientos": extracted.get("estacionamientos"),
                    "m2_construidos": extracted.get("m2_construidos"),
                    "m2_totales": extracted.get("m2_totales"),
                    "superficie_total": extracted.get("superficie_total") or extracted.get("m2_totales"),
                    "superficie_util": extracted.get("superficie_util") or extracted.get("m2_construidos"),
                    "direccion_exacta": extracted.get("direccion_exacta"),
                    "fecha_publicacion": extracted.get("fecha_publicacion") or "",
                    "fecha_publicacion_raw": extracted.get("fecha_publicacion_raw") or "",
                    "publisher_profile_context": extracted.get("publisher_profile_context") or {},
                    "publisher_field_provenance": extracted.get("publisher_field_provenance") or {},
                    "publisher_identity_candidates": extracted.get("publisher_identity_candidates") or [],
                    "details": extracted,
                    "classification": classification,
                    "classification_evidence": evidence,
                    **availability,
                    "extraction_issues": extraction_issues,
                    "html_validation_status": validation["status"],
                    "html_validation_reason": validation["reason"],
                    "html_path": str(downloaded.html_path),
                    "html_source_path": str(downloaded.html_path),
                    "fetch_source": downloaded.fetch_source,
                    "scrape_stage": "processed",
                    "pipeline_state": pipeline_state,
                    "pipeline_complete": schema_pipeline_complete,
                    "classification_debug": {
                        "evidence_strength": classification.get("evidence_strength"),
                        "contradictions": classification.get("contradictions") or [],
                        "requires_review": classification.get("requires_review"),
                        "deepseek_enabled": False, "deepseek_call_executed": False,
                    },
                    "processed_at": timestamp,
                    "updated_at": timestamp,
                    "fecha_scraping": timestamp,
                    "gestion": {
                        "estado": "NUEVO", "estado_captacion": "NUEVO",
                        "ejecutivo_id": None, "ejecutivo_asignado": None,
                        "intent_count": 0, "notas": [], "actividades": [],
                    },
                    "deepseek_call_executed": False,
                    "mongo_write_executed": False,
                }
                records_out.append(result)
                if filter_validation["status"] == "PASA":
                    summary["filter_matches"] += 1
                elif filter_validation["status"] == "FUERA_DE_FILTRO":
                    summary["excluded_out_of_filter"] += 1
                else:
                    summary["pending_filter_validation"] += 1
            except Exception as exc:
                summary["errors_or_blocks"].append(f"detail_error:{row['listing_id']}:{type(exc).__name__}")
                records_out.append({
                    **row, "filter_status": "PENDIENTE_VALIDACION",
                    "filter_validation": {"status": "PENDIENTE_VALIDACION", "reasons": ["detail_extraction_error"]},
                    "classification": {}, "classification_evidence": [],
                    "extraction_issues": [f"detail_error:{type(exc).__name__}"],
                    "detail_error": f"{type(exc).__name__}: {exc}",
                })

    commercial_states = {"DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"}
    summary["publisher_profile_metrics"] = enrich_batch_publisher_profiles(
        records_out,
        config,
        batch_id=batch_id,
    )
    _reclassify_batch_by_exact_profile(records_out)
    class_counts: dict[str, int] = {}
    availability_counts: dict[str, int] = {}
    summary["commercial_candidates"] = []
    for item in records_out:
        state = str((item.get("classification") or {}).get("state") or "unavailable")
        class_counts[state] = class_counts.get(state, 0) + 1
        availability_state = str(item.get("availability_status") or "NO_VERIFICADO")
        availability_counts[availability_state] = availability_counts.get(availability_state, 0) + 1
        if item.get("filter_status") == "PASA" and state in commercial_states:
            summary["commercial_candidates"].append(item["listing_id"])
    summary["preliminary_classification_counts"] = class_counts
    summary["availability_status_counts"] = availability_counts
    summary["commercial_candidate_count"] = len(summary["commercial_candidates"])
    summary["broker_excluded_count"] = sum(
        str((item.get("classification") or {}).get("state") or "").startswith("CORREDOR")
        for item in records_out
    )
    summary["detail_processing_complete"] = len(records_out) == len(new_records) and not any(
        "detail_error:" in error or "listing_blocked:" in error for error in summary["errors_or_blocks"]
    )
    summary["commercial_result_available"] = bool(
        summary["pagination_complete"] and summary["search_filters_verified"]
        and summary["mongo_dedup_verified"] and summary["detail_processing_complete"]
    )
    summary["files"] = _save_search_outputs(summary, records_out, output_dir)
    return summary


def run_pilot(start_url: str | SearchConfig, config: AppConfig | None = None,
              *, batch_id: str | None = None) -> dict[str, Any]:
    """Compatibility adapter; all work runs through the general search engine."""
    config = config or AppConfig()
    if isinstance(start_url, SearchConfig):
        search = start_url
    else:
        search = search_config_from_url(
            start_url,
            output_dir=str(config.reports_dir / "general_search"),
            batch_id=batch_id or "",
            live=True,
        )
    return run_search(search, config)
