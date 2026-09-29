"""V2 dry-run/migration for comparable evidence.

The V2 result is deliberately a field nested under the existing master
property document: ``universo_cartera_prop360.analisis_comparables``.  The
default command is read-only and writes only a local audit JSON.  MongoDB
writes require the explicit ``--write --confirm-migration`` flags and are not
used by the current validation run.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from pymongo import MongoClient, UpdateOne
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.prop360_comparables_cluster_mvp import (  # noqa: E402
    CANONICAL_REGION_BY_COMUNA,
    MAX_BUILT_M2,
    MAX_LAND_M2,
    MARKET_COLLECTION,
    MIN_SURFACE_M2,
    PORTFOLIO_COLLECTION,
    SIMPLE_SURFACE_TYPES,
    SUPPORTED_TYPES,
    canonical_region,
    market_projection,
    normalize_market,
    normalize_portfolio,
    percentile,
    portfolio_query,
    slugify,
    stats,
    to_float,
    valid_count,
    valid_surface,
    normalize_operation,
    normalize_type,
)
from config import Config  # noqa: E402


V2_VERSION = "cluster_v2_20260921"
OUTPUT_COLLECTION = "prop360_comparables_cluster"
MODEL_VERSION_V1 = "cluster_mvp_20260921_v1"
MAX_SELECTED = 20
MIN_CLEAN_FOR_LEVEL_A = 8
MIN_KMEANS_ROWS = 15
MIN_CLIENT_COMPARABLES = 5
NETWORK_ONLY_TYPES = {"oficina", "local_comercial", "bodega", "industrial", "estacionamiento"}
REPORT_DIR = ROOT / "reports"
BACKUP_DIR = ROOT / "backups"


# Canonical commune centroids used only for geographic proximity.  Values are
# approximate commune-centre coordinates; they are not property addresses and
# are never written back to source documents.
COMMUNE_CENTROIDS: dict[str, tuple[str, float, float]] = {
    "algarrobo": ("Valparaíso", -33.37, -71.67),
    "ancud": ("Los Lagos", -41.87, -73.83),
    "arauco": ("Biobío", -37.25, -73.32),
    "buin": ("Región Metropolitana", -33.73, -70.74),
    "bulnes": ("Ñuble", -36.74, -72.30),
    "cartagena": ("Valparaíso", -33.55, -71.61),
    "cauquenes": ("Maule", -35.97, -72.32),
    "cerrillos": ("Región Metropolitana", -33.50, -70.72),
    "chillan": ("Ñuble", -36.60, -72.10),
    "chillan-viejo": ("Ñuble", -36.63, -72.14),
    "chonchi": ("Los Lagos", -42.62, -73.77),
    "cobquecura": ("Ñuble", -36.13, -72.78),
    "coihueco": ("Ñuble", -36.63, -71.83),
    "coinco": ("O'Higgins", -34.27, -70.96),
    "colbun": ("Maule", -35.69, -71.41),
    "colina": ("Región Metropolitana", -33.20, -70.67),
    "concepcion": ("Biobío", -36.83, -73.05),
    "conchali": ("Región Metropolitana", -33.38, -70.68),
    "concon": ("Valparaíso", -32.92, -71.51),
    "coquimbo": ("Coquimbo", -29.95, -71.34),
    "curacavi": ("Región Metropolitana", -33.40, -71.13),
    "curico": ("Maule", -34.98, -71.24),
    "donihue": ("O'Higgins", -34.22, -70.96),
    "el-bosque": ("Región Metropolitana", -33.57, -70.68),
    "el-monte": ("Región Metropolitana", -33.68, -70.98),
    "el-quisco": ("Valparaíso", -33.40, -71.70),
    "empedrado": ("Maule", -35.60, -72.28),
    "estacion-central": ("Región Metropolitana", -33.46, -70.70),
    "huechuraba": ("Región Metropolitana", -33.36, -70.64),
    "independencia": ("Región Metropolitana", -33.42, -70.65),
    "la-cisterna": ("Región Metropolitana", -33.54, -70.66),
    "la-florida": ("Región Metropolitana", -33.52, -70.60),
    "la-granja": ("Región Metropolitana", -33.54, -70.63),
    "la-higuera": ("Coquimbo", -29.50, -71.27),
    "la-pintana": ("Región Metropolitana", -33.58, -70.63),
    "la-reina": ("Región Metropolitana", -33.45, -70.53),
    "la-serena": ("Coquimbo", -29.90, -71.25),
    "la-union": ("Los Ríos", -40.29, -73.08),
    "laja": ("Biobío", -37.28, -72.71),
    "lampa": ("Región Metropolitana", -33.28, -70.90),
    "las-condes": ("Región Metropolitana", -33.41, -70.58),
    "licanten": ("Maule", -34.99, -72.03),
    "linares": ("Maule", -35.85, -71.59),
    "lo-barnechea": ("Región Metropolitana", -33.35, -70.52),
    "lo-prado": ("Región Metropolitana", -33.45, -70.73),
    "longavi": ("Maule", -35.97, -71.68),
    "los-andes": ("Valparaíso", -32.83, -70.60),
    "los-angeles": ("Biobío", -37.47, -72.35),
    "los-lagos": ("Los Ríos", -39.86, -72.83),
    "los-sauces": ("Araucanía", -38.05, -72.83),
    "machali": ("O'Higgins", -34.18, -70.65),
    "macul": ("Región Metropolitana", -33.49, -70.60),
    "maipu": ("Región Metropolitana", -33.51, -70.76),
    "maule": ("Maule", -35.52, -71.69),
    "melipilla": ("Región Metropolitana", -33.69, -71.22),
    "molina": ("Maule", -35.12, -71.28),
    "niquen": ("Ñuble", -36.29, -71.90),
    "nunoa": ("Región Metropolitana", -33.46, -70.60),
    "osorno": ("Los Lagos", -40.57, -73.13),
    "padre-las-casas": ("Araucanía", -38.77, -72.59),
    "paine": ("Región Metropolitana", -33.81, -70.74),
    "papudo": ("Valparaíso", -32.51, -71.45),
    "paredones": ("O'Higgins", -34.65, -72.54),
    "parral": ("Maule", -36.14, -71.83),
    "pedro-aguirre-cerda": ("Región Metropolitana", -33.49, -70.68),
    "pelluhue": ("Maule", -35.81, -72.58),
    "penaflor": ("Región Metropolitana", -33.61, -70.91),
    "penalolen": ("Región Metropolitana", -33.49, -70.54),
    "pencahue": ("Maule", -35.39, -71.81),
    "pichilemu": ("O'Higgins", -34.39, -72.00),
    "pinto": ("Ñuble", -36.71, -71.90),
    "providencia": ("Región Metropolitana", -33.43, -70.61),
    "pudahuel": ("Región Metropolitana", -33.44, -70.75),
    "puente-alto": ("Región Metropolitana", -33.61, -70.58),
    "puerto-montt": ("Los Lagos", -41.47, -72.94),
    "puerto-varas": ("Los Lagos", -41.32, -72.99),
    "quilicura": ("Región Metropolitana", -33.36, -70.73),
    "quillon": ("Ñuble", -36.74, -72.47),
    "quillota": ("Valparaíso", -32.88, -71.25),
    "quilpue": ("Valparaíso", -33.05, -71.44),
    "rancagua": ("O'Higgins", -34.17, -70.74),
    "recoleta": ("Región Metropolitana", -33.40, -70.64),
    "renca": ("Región Metropolitana", -33.40, -70.73),
    "rio-claro": ("Maule", -35.28, -71.26),
    "san-antonio": ("Valparaíso", -33.59, -71.61),
    "san-bernardo": ("Región Metropolitana", -33.59, -70.70),
    "san-carlos": ("Ñuble", -36.42, -71.96),
    "san-clemente": ("Maule", -35.54, -71.49),
    "san-ignacio": ("Ñuble", -36.80, -72.05),
    "san-javier": ("Maule", -35.60, -71.74),
    "san-jose-de-maipo": ("Región Metropolitana", -33.64, -70.35),
    "san-miguel": ("Región Metropolitana", -33.49, -70.65),
    "san-nicolas": ("Ñuble", -36.50, -72.21),
    "santiago": ("Región Metropolitana", -33.45, -70.65),
    "santo-domingo": ("Valparaíso", -33.64, -71.62),
    "talagante": ("Región Metropolitana", -33.66, -70.93),
    "talca": ("Maule", -35.43, -71.66),
    "temuco": ("Araucanía", -38.74, -72.59),
    "valparaiso": ("Valparaíso", -33.05, -71.62),
    "villa-alegre": ("Maule", -35.60, -71.74),
    "villarrica": ("Araucanía", -39.28, -72.23),
    "vina-del-mar": ("Valparaíso", -33.02, -71.55),
    "vitacura": ("Región Metropolitana", -33.39, -70.57),
}


def _finite(value: Any) -> float | None:
    number = to_float(value)
    return number if number is not None and math.isfinite(number) else None


def _haversine_km(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> float | None:
    values = [_finite(lat1), _finite(lon1), _finite(lat2), _finite(lon2)]
    if any(value is None for value in values):
        return None
    lat1_f, lon1_f, lat2_f, lon2_f = [math.radians(float(value)) for value in values]
    dlat = lat2_f - lat1_f
    dlon = lon2_f - lon1_f
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_f) * math.cos(lat2_f) * math.sin(dlon / 2) ** 2
    return round(6_371.0 * 2 * math.asin(math.sqrt(a)), 3)


def _geo_for(comuna_slug: str, region: str | None = None) -> tuple[str, float | None, float | None, str]:
    centroid = COMMUNE_CENTROIDS.get(comuna_slug)
    if centroid:
        return centroid[0], centroid[1], centroid[2], "COMMUNE_CENTROID"
    return region or CANONICAL_REGION_BY_COMUNA.get(comuna_slug) or "N/A", None, None, "MISSING"


def _add_geo(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    region, lat, lon, geo_source = _geo_for(str(row.get("comuna_slug") or ""), row.get("region"))
    enriched["region"] = region
    enriched["lat"] = lat
    enriched["lon"] = lon
    enriched["geo_source"] = geo_source
    return enriched


def _json_safe(value: Any) -> Any:
    """Convert Mongo/Python values into deterministic local-audit JSON."""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _document_hash(document: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()


def _document_without_analysis(document: dict[str, Any]) -> dict[str, Any]:
    safe = copy.deepcopy(_json_safe(document))
    if isinstance(safe, dict):
        safe.pop("analisis_comparables", None)
    return safe


def _write_local_snapshot(raw_documents: list[dict[str, Any]], timestamp: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKUP_DIR / f"prop360_universo_cartera_v2_before_{timestamp}.json"
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "collection": PORTFOLIO_COLLECTION,
        "purpose": "local_before_snapshot_for_analisis_comparables_v2_migration",
        "target_count": len(raw_documents),
        "documents": [
            {
                "codigo": str(document.get("codigo")),
                "_id": str(document.get("_id")),
                "document_hash_before": _document_hash(document),
                "analisis_comparables_before": _json_safe(document.get("analisis_comparables")),
                "document_before": _json_safe(document),
            }
            for document in raw_documents
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _network_projection() -> dict[str, int]:
    return {
        "_id": 0,
        "codigo": 1,
        "oficina_nombre": 1,
        "oficina_id": 1,
        "estado": 1,
        "disponible_prop360": 1,
        "tipo_operacion": 1,
        "metadata": 1,
        "ubicacion": 1,
        "caracteristicas": 1,
        "resumen": 1,
    }


def _normalize_market_source(raw: dict[str, Any]) -> dict[str, Any] | None:
    row = normalize_market(raw)
    if row["operation"] not in {"venta", "arriendo"} or row["tipo_normalizado"] not in SUPPORTED_TYPES:
        return None
    if row["comuna_slug"] == "N/A" or not (row.get("price") or {}).get("valid"):
        return None
    if row.get("validation_status") in {"BLOCKED", "LISTING_REMOVED"}:
        return None
    row = _add_geo(row)
    row.update(
        {
            "source": "SCRAPING",
            "source_id": str(row.get("listing_id")),
            "portal": str(row.get("portal") or "N/A"),
            "office": None,
        }
    )
    return row


def _normalize_network_source(raw: dict[str, Any]) -> dict[str, Any] | None:
    available = raw.get("disponible_prop360") is True or (raw.get("estado") or {}).get("disponible_prop360") is True
    if not available:
        return None
    row = normalize_portfolio(raw)
    if row["operation"] not in {"venta", "arriendo"} or row["tipo_normalizado"] not in SUPPORTED_TYPES:
        return None
    if row["comuna_slug"] == "N/A" or not (row.get("price") or {}).get("valid"):
        return None
    row = _add_geo(row)
    row.update(
        {
            "source": "PROCASA_NETWORK",
            "source_id": str(row.get("codigo")),
            "portal": "PROCASA_NETWORK",
            "office": raw.get("oficina_nombre") or raw.get("oficina_id") or (raw.get("estado") or {}).get("oficina"),
            "listing_id": str(row.get("codigo")),
        }
    )
    return row


def load_target_documents(db: Any) -> list[dict[str, Any]]:
    return list(db[PORTFOLIO_COLLECTION].find(portfolio_query()).sort("codigo", 1))


def normalize_target_document(raw: dict[str, Any]) -> dict[str, Any]:
    target = _add_geo(normalize_portfolio(raw))
    target["office"] = "PROCASA SUCRE"
    target["_mongo_id"] = raw.get("_id")
    return target


def load_portfolio_targets(db: Any) -> list[dict[str, Any]]:
    return [normalize_target_document(raw) for raw in load_target_documents(db)]


def load_market_sources(db: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in db[MARKET_COLLECTION].find({}, market_projection(include_text=False)).batch_size(1000):
        row = _normalize_market_source(raw)
        if row is None:
            continue
        key = (row["source"], row["portal"], row["source_id"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def load_network_sources(db: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    query = {
        "$and": [
            {"$or": [{"estado.disponible_prop360": True}, {"disponible_prop360": True}]},
            {"$or": [{"estado.estado_prop360": "Activa"}, {"resumen.estado_prop360": "Activa"}]},
        ]
    }
    for raw in db[PORTFOLIO_COLLECTION].find(query, _network_projection()).batch_size(500):
        row = _normalize_network_source(raw)
        if row is None or row["source_id"] in seen:
            continue
        seen.add(row["source_id"])
        rows.append(row)
    return rows


def _compatible(target: dict[str, Any], row: dict[str, Any]) -> bool:
    return target.get("operation") == row.get("operation") and target.get("tipo_normalizado") == row.get("tipo_normalizado")


def _dedup_rows(rows: Iterable[dict[str, Any]], target_code: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if str(row.get("source_id")) == target_code and row.get("source") == "PROCASA_NETWORK":
            continue
        key = (str(row.get("source")), str(row.get("source_id")))
        if key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


def _candidate_pool(target: dict[str, Any], market_rows: list[dict[str, Any]], network_rows: list[dict[str, Any]]) -> dict[str, Any]:
    target_code = str(target.get("codigo"))
    market = _dedup_rows((row for row in market_rows if _compatible(target, row)), target_code)
    network = _dedup_rows((row for row in network_rows if _compatible(target, row)), target_code)
    if target["tipo_normalizado"] in NETWORK_ONLY_TYPES:
        primary = network
        source_policy = "PROCASA_NETWORK_ONLY"
    else:
        primary = market
        source_policy = "SCRAPING_PREFERRED_NETWORK_COMPLEMENT"

    exact_primary = [row for row in primary if row.get("comuna_slug") == target.get("comuna_slug")]
    exact_combined = _dedup_rows(exact_primary + [row for row in network if row.get("comuna_slug") == target.get("comuna_slug")], target_code)
    if len(exact_primary) >= MIN_CLEAN_FOR_LEVEL_A:
        return {
            "geographic_level": "COMMUNE",
            "rows": exact_primary,
            "n_raw": len(exact_primary),
            "source_policy": source_policy,
            "reason": "same_commune_has_at_least_8_clean_candidates",
        }
    if len(exact_combined) >= MIN_CLEAN_FOR_LEVEL_A:
        return {
            "geographic_level": "COMMUNE",
            "rows": exact_combined,
            "n_raw": len(exact_combined),
            "source_policy": "SCRAPING_PLUS_PROCASA_NETWORK",
            "reason": "same_commune_reached_8_after_network_complement",
        }

    return {
        "geographic_level": "COMMUNE",
        "rows": exact_combined,
        "n_raw": len(exact_combined),
        "source_policy": "SCRAPING_PLUS_PROCASA_NETWORK" if exact_combined != exact_primary else source_policy,
        "reason": "same_commune_only_below_8; no regional substitution",
    }


def _choose_features(target: dict[str, Any], rows: list[dict[str, Any]], geographic_level: str) -> tuple[list[str], str | None, list[str]]:
    tipo = target.get("tipo_normalizado")
    reasons: list[str] = []
    if not rows:
        return [], None, ["no_candidate_rows"]
    if target.get("bedrooms") is None or target.get("bathrooms") is None:
        return [], None, ["target_missing_required_bedrooms_or_bathrooms"]

    def coverage(field: str) -> float:
        return sum(row.get(field) is not None for row in rows) / max(len(rows), 1)

    def usable(field: str) -> int:
        return sum(row.get(field) is not None for row in rows)

    if tipo == "casa":
        if target.get("built_m2") is not None and usable("built_m2") >= 5:
            primary = "built_m2"
        elif target.get("land_m2") is not None and usable("land_m2") >= 5:
            primary = "land_m2"
            reasons.append("built_m2_target_present_but_built_coverage_below_5;land_m2_used_explicitly")
        else:
            return [], None, ["house_has_no_usable_built_or_land_surface"]
        features = [primary]
        if primary == "built_m2" and target.get("land_m2") is not None and usable("land_m2") >= 5:
            features.append("land_m2")
        elif primary == "land_m2" and target.get("built_m2") is not None:
            reasons.append("built_m2_not_used_due_to_insufficient_candidate_coverage")
        label = "built_m2" if primary == "built_m2" else "land_m2"
    elif tipo == "departamento":
        if target.get("surface_ref_m2") is None:
            return [], None, ["department_has_no_surface_ref_m2"]
        if usable("surface_ref_m2") < 5:
            return [], None, ["department_has_less_than_5_surface_candidates"]
        features = ["surface_ref_m2"]
        label = "surface_ref_m2"
    elif tipo in {"parcela", "sitio"}:
        if target.get("land_m2") is None:
            return [], None, ["land_property_has_no_land_m2"]
        if usable("land_m2") < 5:
            return [], None, ["land_property_has_less_than_5_surface_candidates"]
        features = ["land_m2"]
        label = "land_m2"
    elif tipo in SIMPLE_SURFACE_TYPES:
        if target.get("surface_ref_m2") is None:
            return [], None, ["simple_type_has_no_surface_ref_m2"]
        if usable("surface_ref_m2") < 5:
            return [], None, ["simple_type_has_less_than_5_surface_candidates"]
        features = ["surface_ref_m2"]
        label = "surface_ref_m2"
    else:
        return [], None, ["unsupported_type"]

    # Bedrooms and bathrooms are required candidate dimensions. Parking is
    # deliberately omitted from the complete feature vector so missing
    # candidate parking never removes a row; it is added pairwise below only
    # when both values are actually present.
    features.extend(("bedrooms", "bathrooms"))
    if target.get("parking") is not None and coverage("parking") > 0:
        reasons.append("parking_used_as_optional_pairwise_signal")
    if geographic_level == "REGION_NEARBY" and target.get("lat") is not None and target.get("lon") is not None:
        # Geographic distance is measured from the target property itself.
        # Keep the target value explicit so the regional fallback can build a
        # complete feature vector without confusing this derived feature with
        # a missing structural attribute.
        target["geo_distance_km"] = 0.0
        for row in rows:
            row["geo_distance_km"] = _haversine_km(target.get("lat"), target.get("lon"), row.get("lat"), row.get("lon"))
        if sum(row.get("geo_distance_km") is not None for row in rows) >= 5:
            features.append("geo_distance_km")
        else:
            reasons.append("geographic_distance_missing_for_too_many_candidates")
    return features, label, reasons


def _fit_and_select(target: dict[str, Any], rows: list[dict[str, Any]], features: list[str]) -> dict[str, Any]:
    model_rows = [row for row in rows if all(row.get(feature) is not None for feature in features)]
    if len(model_rows) < MIN_CLIENT_COMPARABLES:
        return {"ok": False, "rows": [], "n_model": len(model_rows), "reason": "less_than_5_complete_structural_candidates"}
    matrix = np.array([[float(row[feature]) for feature in features] for row in model_rows], dtype=float)
    transformed = matrix.copy()
    for index, feature in enumerate(features):
        if feature.endswith("_m2"):
            transformed[:, index] = np.log1p(np.maximum(transformed[:, index], 0))
    scaler = StandardScaler().fit(transformed)
    scaled = scaler.transform(transformed)
    target_matrix = np.array([[float(target[feature]) for feature in features]], dtype=float)
    for index, feature in enumerate(features):
        if feature.endswith("_m2"):
            target_matrix[:, index] = np.log1p(np.maximum(target_matrix[:, index], 0))
    target_scaled = scaler.transform(target_matrix)[0]

    target_parking = _finite(target.get("parking"))
    known_parking = [_finite(row.get("parking")) for row in model_rows]
    parking_values = [value for value in known_parking if value is not None]
    if target_parking is not None:
        parking_values.append(target_parking)
    parking_scale = float(np.std(parking_values)) if len(parking_values) > 1 else 0.0

    def similarity_distance(index: int) -> float:
        structural = float(np.linalg.norm(scaled[index] - target_scaled))
        candidate_parking = known_parking[index]
        if target_parking is None or candidate_parking is None:
            return structural
        scale = parking_scale if parking_scale > 1e-9 else 1.0
        parking_delta = (candidate_parking - target_parking) / scale
        return math.hypot(structural, parking_delta)

    selected_model: dict[str, Any] | None = None
    if len(model_rows) >= MIN_KMEANS_ROWS:
        max_k = min(5, len(model_rows) // 5)
        for k in range(2, max_k + 1):
            model = KMeans(n_clusters=k, random_state=42, n_init=10)
            labels = model.fit_predict(scaled)
            sizes = Counter(int(label) for label in labels)
            if min(sizes.values()) < 3:
                continue
            score = float(silhouette_score(scaled, labels))
            candidate = {"model": model, "labels": labels, "silhouette": score, "k": k, "sizes": sizes}
            if selected_model is None or (score, -k) > (selected_model["silhouette"], -selected_model["k"]):
                selected_model = candidate

    if selected_model is not None:
        predicted = int(selected_model["model"].predict([target_scaled])[0])
        indexed = [
            (row, similarity_distance(index))
            for index, row in enumerate(model_rows)
            if int(selected_model["labels"][index]) == predicted
        ]
        if len(indexed) >= MIN_CLIENT_COMPARABLES:
            return {
                "ok": True,
                "method": "kmeans",
                "cluster_id": predicted,
                "k": selected_model["k"],
                "silhouette": round(selected_model["silhouette"], 6),
                "cluster_size": len(indexed),
                "n_model": len(model_rows),
                "rows": sorted(indexed, key=lambda item: item[1]),
                "scaled": True,
            }

    nearest = [(row, similarity_distance(index)) for index, row in enumerate(model_rows)]
    return {
        "ok": True,
        "method": "nearest_neighbors_fallback",
        "cluster_id": None,
        "k": None,
        "silhouette": None,
        "cluster_size": len(nearest),
        "n_model": len(model_rows),
        "rows": sorted(nearest, key=lambda item: item[1]),
        "scaled": True,
    }


def _unit_price(row: dict[str, Any], operation: str) -> float | None:
    price = row.get("price") or {}
    return _finite(price.get("price_uf" if operation == "venta" else "price_clp"))


def _primary_surface(target: dict[str, Any], row: dict[str, Any], primary: str | None) -> float | None:
    if primary == "built_m2":
        return _finite(row.get("built_m2"))
    if primary == "land_m2":
        return _finite(row.get("land_m2"))
    return _finite(row.get("surface_ref_m2"))


def _price_m2(row: dict[str, Any], operation: str, primary: str | None) -> float | None:
    unit = _unit_price(row, operation)
    surface = _primary_surface({}, row, primary)
    return unit / surface if unit is not None and surface and surface > 0 else None


def _distribution(values: list[float], property_value: float | None) -> dict[str, Any] | None:
    if not values:
        return None
    return stats(values, property_value)


def _format_type_indicator(tipo: str, operation: str, primary: str | None) -> str:
    unit = "UF/m²" if operation == "venta" else "CLP/m²"
    if tipo == "casa" and primary == "built_m2":
        return f"{unit} construido"
    if tipo == "casa" and primary == "land_m2":
        return f"{unit} terreno"
    if tipo == "departamento":
        return f"{unit} útil/construido"
    if tipo in {"parcela", "sitio"}:
        return f"{unit} terreno"
    return unit


def _interpretation(pctl: float | None, selected_n: int, indicator: str) -> str:
    if selected_n < 5 or pctl is None:
        return "Muestra limitada; los comparables se entregan como referencia orientativa y no permiten una conclusión fuerte."
    if pctl < 30:
        text = "Posicionamiento competitivo frente a publicaciones similares."
    elif pctl <= 60:
        text = "Posicionamiento alineado con el mercado comparable."
    elif pctl <= 75:
        text = "Posicionamiento en la zona media-alta del grupo comparable."
    elif pctl <= 90:
        text = "Posicionamiento por sobre gran parte de las publicaciones comparables."
    else:
        text = "Posicionamiento en la zona superior de precios del grupo comparable."
    return f"{text} Referencia: {indicator}; son publicaciones observadas, no precios de cierre ni transacciones."


def _comparable_payload(row: dict[str, Any], distance: float, operation: str, primary: str | None) -> dict[str, Any]:
    price = row.get("price") or {}
    unit_price = _unit_price(row, operation)
    built = _finite(row.get("built_m2"))
    useful = _finite(row.get("useful_m2"))
    land = _finite(row.get("land_m2"))
    built_m2 = unit_price / built if unit_price is not None and built else None
    land_m2 = unit_price / land if unit_price is not None and land else None
    return {
        "listing_id": row.get("listing_id") or row.get("source_id"),
        "portal": row.get("portal"),
        "source": row.get("source"),
        "source_id": row.get("source_id"),
        "precio_uf": price.get("price_uf"),
        "precio_clp": price.get("price_clp"),
        "superficie_construida": built,
        "superficie_util": useful,
        "superficie_terreno": land,
        "dormitorios": row.get("bedrooms"),
        "banos": row.get("bathrooms"),
        "estacionamientos": row.get("parking"),
        "precio_m2": _price_m2(row, operation, primary),
        "precio_m2_construido": built_m2,
        "precio_m2_terreno": land_m2,
        "uf_m2_built": built_m2 if operation == "venta" else None,
        "uf_m2_land": land_m2 if operation == "venta" else None,
        "distance": round(float(distance), 6),
        "geo_distance_km": row.get("geo_distance_km"),
    }


def _v2_for_target(target: dict[str, Any], market_rows: list[dict[str, Any]], network_rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    segment = {
        "operacion": target.get("operation"),
        "tipo_normalizado": target.get("tipo_normalizado"),
        "comuna_slug": target.get("comuna_slug"),
        "region": target.get("region"),
    }
    base_reasons: list[str] = []
    if target.get("operation") not in {"venta", "arriendo"}:
        base_reasons.append("operacion_no_normalizada_o_dual")
    if target.get("tipo_normalizado") not in SUPPORTED_TYPES:
        base_reasons.append("tipo_no_soportado")
    if not (target.get("price") or {}).get("valid"):
        base_reasons.append("precio_objetivo_invalido")
    target_base_invalid = bool(base_reasons)

    pool = _candidate_pool(target, market_rows, network_rows)
    rows = list(pool["rows"])
    n_raw = len(rows)
    features, primary, feature_reasons = _choose_features(target, rows, pool["geographic_level"])
    base_reasons.extend(feature_reasons)
    model = _fit_and_select(target, rows, features) if features and not target_base_invalid else {"ok": False, "reason": "target_base_data_invalid", "rows": [], "n_model": 0}
    if not model.get("ok"):
        base_reasons.append(model.get("reason", "model_not_available"))

    selected_pairs = (model.get("rows") or [])[:MAX_SELECTED]
    selected_rows = [row for row, _distance in selected_pairs]
    selected_n = len(selected_pairs)
    operation = target.get("operation")
    price_values = [_unit_price(row, operation) for row in selected_rows]
    price_values = [value for value in price_values if value is not None]
    property_price = _unit_price(target, operation)
    m2_values = [_price_m2(row, operation, primary) for row in selected_rows]
    m2_values = [value for value in m2_values if value is not None]
    property_m2 = _price_m2(target, operation, primary)
    price_distribution = _distribution(price_values, property_price) if selected_n >= MIN_CLIENT_COMPARABLES else None
    m2_distribution = _distribution(m2_values, property_m2) if selected_n >= MIN_CLIENT_COMPARABLES else None
    if selected_n >= 15:
        evidence_level = "HIGH"
    elif selected_n >= 8:
        evidence_level = "MEDIUM"
    elif selected_n >= 5:
        evidence_level = "LIMITED"
    else:
        evidence_level = "INSUFFICIENT"
    if selected_n < MIN_CLIENT_COMPARABLES:
        base_reasons.append("menos_de_5_vecinos_estructurales")
    indicator = _format_type_indicator(target.get("tipo_normalizado"), operation, primary)
    comparable_payloads = [
        _comparable_payload(row, distance, operation, primary)
        for row, distance in selected_pairs
    ]
    max_geo = max((row.get("geo_distance_km") for row in selected_rows if row.get("geo_distance_km") is not None), default=None)
    source_counts = Counter(row.get("source") for row in selected_rows)
    status = "INSUFFICIENT_PROPERTY_DATA" if any(reason in base_reasons for reason in ("precio_objetivo_invalido", "house_has_no_usable_built_or_land_surface", "department_has_no_surface_ref_m2", "land_property_has_no_land_m2", "simple_type_has_no_surface_ref_m2")) else "INSUFFICIENT_COMPARABLES" if selected_n < MIN_CLIENT_COMPARABLES else "CLUSTERED" if model.get("method") == "kmeans" else "NEAREST_NEIGHBORS_FALLBACK"
    analysis = {
        "version": V2_VERSION,
        "generated_at": None,
        "status": status,
        "segmento": segment,
        "modelo": {
            "method": model.get("method"),
            "cluster_id": model.get("cluster_id"),
            "k": model.get("k"),
            "silhouette": model.get("silhouette"),
            "features_used": features,
            "geographic_level": pool["geographic_level"],
        },
        "mercado": {
            "geographic_level": pool["geographic_level"],
            "geographic_selection_reason": pool["reason"],
            "n_raw": n_raw,
            "n_clean": len(rows),
            "n_model": model.get("n_model", 0),
            "n_cluster": model.get("cluster_size", 0),
            "n_selected": selected_n,
            "max_geographic_distance_km": round(float(max_geo), 3) if max_geo is not None else None,
            "source_counts": {str(key): int(value) for key, value in source_counts.items()},
            "price_distribution": price_distribution,
            "price_m2_distribution": m2_distribution,
            "property_price": property_price,
            "property_percentile": percentile(property_price, price_values) if price_values else None,
            "property_price_m2": property_m2,
            "price_surface": primary,
        },
        "comparables": comparable_payloads,
        "client_evidence": {
            "evidence_level": evidence_level,
            "interpretation": _interpretation(
                m2_distribution.get("property_percentile") if m2_distribution else None,
                selected_n,
                indicator,
            ),
            "primary_indicator": indicator,
            "top_3_comparables": comparable_payloads[:3],
            "publication_disclaimer": "Publicaciones comparables observadas en el mercado; no son precios de cierre ni transacciones.",
        },
        "data_quality": {
            "level": evidence_level,
            "reasons": sorted(set(base_reasons)),
            "source_policy": pool["source_policy"],
            "geographic_data_available": target.get("lat") is not None,
            "proximity_in_model": "geo_distance_km" in features,
        },
    }
    return analysis, {
        "codigo": str(target.get("codigo")),
        "tipo": target.get("tipo_normalizado"),
        "operacion": target.get("operation"),
        "region": target.get("region"),
        "comuna": target.get("comuna_slug"),
        "status": status,
        "evidence_level": evidence_level,
        "n_raw": n_raw,
        "n_clean": len(rows),
        "n_selected": selected_n,
        "geographic_level": pool["geographic_level"],
        "source_counts": dict(source_counts),
        "reasons": sorted(set(base_reasons)),
    }


def _v1_audit_by_code(db: Any) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for doc in db[OUTPUT_COLLECTION].find(
        {"model_version": MODEL_VERSION_V1},
        {"_id": 0, "codigo": 1, "status": 1, "data_quality": 1, "segmento": 1, "client_evidence": 1},
    ):
        quality = doc.get("data_quality") or {}
        evidence = doc.get("client_evidence") or {}
        output[str(doc.get("codigo"))] = {
            "status": doc.get("status"),
            "evidence_level": evidence.get("evidence_level"),
            "reasons": quality.get("reasons") or [],
        }
    return output


def _write_analysis(db: Any, targets: list[dict[str, Any]], analyses: dict[str, dict[str, Any]]) -> dict[str, int]:
    operations = []
    for target in targets:
        code = str(target.get("codigo"))
        mongo_id = target.get("_mongo_id")
        if mongo_id is None or code not in analyses:
            raise RuntimeError(f"Falta _id o análisis para código {code}")
        operations.append(
            UpdateOne(
                {"_id": mongo_id},
                {"$set": {"analisis_comparables": analyses[code]}},
                upsert=False,
            )
        )
    matched = modified = upserted = 0
    for start in range(0, len(operations), 200):
        result = db[PORTFOLIO_COLLECTION].bulk_write(operations[start : start + 200], ordered=False)
        matched += int(result.matched_count)
        modified += int(result.modified_count)
        upserted += int(result.upserted_count)
    return {"matched": matched, "modified": modified, "upserted": upserted}


def _post_write_audit(
    db: Any,
    raw_before: list[dict[str, Any]],
    analyses: dict[str, dict[str, Any]],
    sample_size: int = 30,
) -> dict[str, Any]:
    before_by_code = {str(document.get("codigo")): document for document in raw_before}
    ids = [document.get("_id") for document in raw_before]
    after_documents = list(db[PORTFOLIO_COLLECTION].find({"_id": {"$in": ids}}))
    after_by_code = {str(document.get("codigo")): document for document in after_documents}
    errors: list[str] = []
    if len(after_by_code) != len(before_by_code):
        errors.append(f"after_document_count={len(after_by_code)} expected={len(before_by_code)}")

    unexpected_all: list[str] = []
    for code, before in before_by_code.items():
        after = after_by_code.get(code)
        if after is None:
            unexpected_all.append(f"missing_after:{code}")
            continue
        if _canonical_json(_document_without_analysis(before)) != _canonical_json(_document_without_analysis(after)):
            unexpected_all.append(code)
    if unexpected_all:
        errors.append(f"unexpected_fields_modified={unexpected_all[:10]}")

    sample_rng = random.Random(20260921)
    sample_codes = sorted(before_by_code)
    sample_rng.shuffle(sample_codes)
    sample_codes = sample_codes[:sample_size]
    sample_failures = [code for code in sample_codes if code in unexpected_all]

    version_docs = list(
        db[PORTFOLIO_COLLECTION].find(
            {"analisis_comparables.version": V2_VERSION},
            {"_id": 0, "codigo": 1, "analisis_comparables": 1},
        )
    )
    written_codes = {str(document.get("codigo")) for document in version_docs}
    expected_codes = set(analyses)
    if written_codes != expected_codes:
        errors.append(f"version_code_set_mismatch written={len(written_codes)} expected={len(expected_codes)}")
    if any(document.get("analisis_comparables", {}).get("version") != V2_VERSION for document in version_docs):
        errors.append("version_mismatch_in_written_documents")

    quality_counts = Counter(
        (document.get("analisis_comparables") or {}).get("client_evidence", {}).get("evidence_level")
        for document in version_docs
    )
    expected_quality = Counter(
        analysis["client_evidence"]["evidence_level"] for analysis in analyses.values()
    )
    if quality_counts != expected_quality:
        errors.append(f"quality_count_mismatch actual={dict(quality_counts)} expected={dict(expected_quality)}")

    target_16469 = after_by_code.get("16469") or {}
    analysis_16469 = target_16469.get("analisis_comparables") or {}
    market_16469 = analysis_16469.get("mercado") or {}
    raw_16469 = before_by_code.get("16469")
    normalized_16469 = normalize_target_document(raw_16469) if raw_16469 else {}
    checks_16469 = {
        "surface_used_m2": normalized_16469.get("surface_ref_m2"),
        "candidate_count": market_16469.get("n_model"),
        "pool_candidate_count": market_16469.get("n_raw"),
        "n_selected": market_16469.get("n_selected"),
        "property_uf_m2": market_16469.get("property_price_m2"),
        "median_uf_m2": ((market_16469.get("price_m2_distribution") or {}).get("median")),
        "bar_expected_visible": int(market_16469.get("n_selected") or 0) >= MIN_CLIENT_COMPARABLES,
    }
    if raw_16469 is not None:
        if analysis_16469.get("version") != V2_VERSION:
            errors.append("16469_missing_expected_v2_analysis")
        if int(market_16469.get("n_selected") or 0) != len(analysis_16469.get("comparables") or []):
            errors.append("16469_selected_count_does_not_match_payload")

    return {
        "target_count": len(before_by_code),
        "analysis_v2_written": len(written_codes),
        "unique_codes": len(written_codes),
        "quality_counts": dict(quality_counts),
        "sample_size": len(sample_codes),
        "sample_pass": not sample_failures,
        "sample_failures": sample_failures,
        "unexpected_fields_modified": unexpected_all,
        "all_documents_non_analysis_unchanged": not unexpected_all,
        "16469": checks_16469,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="V2 comparable analysis migration; dry-run is the default")
    parser.add_argument("--write", action="store_true", help="write analisis_comparables into the master collection")
    parser.add_argument("--confirm-migration", action="store_true", help="required together with --write")
    args = parser.parse_args()
    if args.write and not args.confirm_migration:
        raise SystemExit("Abortado: --write requiere --confirm-migration")
    if not Config.MONGO_URI:
        raise RuntimeError("MONGO_URI no está configurado")
    client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=20_000, connectTimeoutMS=20_000)
    client.admin.command("ping")
    db = client[Config.DB_NAME]
    raw_targets = load_target_documents(db)
    targets = [normalize_target_document(raw) for raw in raw_targets]
    if not targets:
        raise RuntimeError("No se encontraron propiedades objetivo activas y disponibles de PROCASA SUCRE")
    target_codes = [str(target.get("codigo")) for target in targets]
    if len(set(target_codes)) != len(target_codes):
        raise RuntimeError("Los códigos objetivo no son únicos; se aborta antes de escribir")
    market_rows = load_market_sources(db)
    network_rows = load_network_sources(db)
    v1 = _v1_audit_by_code(db)
    generated_at = datetime.now(timezone.utc).isoformat()
    analyses: dict[str, dict[str, Any]] = {}
    audits: list[dict[str, Any]] = []
    for target in targets:
        analysis, audit = _v2_for_target(target, market_rows, network_rows)
        analysis["generated_at"] = generated_at
        analyses[str(target["codigo"])] = analysis
        audit["v1_status"] = (v1.get(str(target["codigo"])) or {}).get("status")
        audit["v1_evidence_level"] = (v1.get(str(target["codigo"])) or {}).get("evidence_level")
        audit["v1_reasons"] = (v1.get(str(target["codigo"])) or {}).get("reasons")
        audits.append(audit)

    counts = Counter(analysis["client_evidence"]["evidence_level"] for analysis in analyses.values())
    before_with_5 = sum(
        int((((raw.get("analisis_comparables") or {}).get("mercado") or {}).get("n_selected")) or 0) >= MIN_CLIENT_COMPARABLES
        for raw in raw_targets
    )
    after_with_5 = sum(
        int(((analysis.get("mercado") or {}).get("n_selected")) or 0) >= MIN_CLIENT_COMPARABLES
        for analysis in analyses.values()
    )
    insufficient_after = len(analyses) - after_with_5
    target_16469 = next((target for target in targets if str(target.get("codigo")) == "16469"), None)
    analysis_16469 = analyses.get("16469") or {}
    market_16469 = analysis_16469.get("mercado") or {}
    distribution_16469 = market_16469.get("price_m2_distribution") or {}
    control_16469 = {
        "surface_used_m2": target_16469.get("surface_ref_m2") if target_16469 else None,
        "candidate_count": market_16469.get("n_model"),
        "pool_candidate_count": market_16469.get("n_raw"),
        "selected_count": market_16469.get("n_selected"),
        "median_uf_m2": distribution_16469.get("median"),
        "target_uf_m2": market_16469.get("property_price_m2"),
        "bar_expected_visible": int(market_16469.get("n_selected") or 0) >= MIN_CLIENT_COMPARABLES,
    }
    by_type_total = Counter(audit["tipo"] for audit in audits)
    by_type_usable = Counter(audit["tipo"] for audit in audits if audit["evidence_level"] in {"HIGH", "MEDIUM", "LIMITED"})
    reason_counts = Counter(reason for audit in audits for reason in audit["reasons"])
    v1_level_counts = Counter((v1.get(code) or {}).get("evidence_level") for code in analyses)

    def grouped_reason_counts(reason_field: str) -> dict[str, int]:
        grouped: Counter[str] = Counter()
        for audit in audits:
            for reason in audit.get(reason_field) or []:
                key = "|".join(
                    [
                        str(audit.get("tipo") or "N/A"),
                        str(audit.get("operacion") or "N/A"),
                        str(audit.get("region") or "N/A"),
                        str(audit.get("comuna") or "N/A"),
                        str(reason),
                    ]
                )
                grouped[key] += 1
        return dict(sorted(grouped.items()))

    v1_grouped_reason_counts = grouped_reason_counts("v1_reasons")
    v2_grouped_reason_counts = grouped_reason_counts("reasons")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"prop360_comparables_v2_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_payload: dict[str, Any] = {
        "version": V2_VERSION,
        "generated_at": generated_at,
        "read_only": not args.write,
        "target_count": len(targets),
        "source_counts": {"scraping": len(market_rows), "procasa_network": len(network_rows)},
        "v1_evidence_level_counts": dict(v1_level_counts),
        "v2_evidence_level_counts": dict(counts),
        "v2_audit_by_property": audits,
        "v1_grouped_reason_counts": v1_grouped_reason_counts,
        "v2_grouped_reason_counts": v2_grouped_reason_counts,
        "v2_reason_counts": dict(reason_counts),
        "comparable_counts": {
            "with_5_or_more_before": before_with_5,
            "with_5_or_more_after": after_with_5,
            "still_insufficient_after": insufficient_after,
        },
        "property_16469": control_16469,
    }
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"TOTAL_TARGET={len(targets)}")
    print(f"V1_CLIENT_USABLE={int(v1_level_counts.get('HIGH', 0) + v1_level_counts.get('MEDIUM', 0))}")
    print(f"V2_PROJECTED_HIGH={counts.get('HIGH', 0)}")
    print(f"V2_PROJECTED_MEDIUM={counts.get('MEDIUM', 0)}")
    print(f"V2_PROJECTED_LIMITED={counts.get('LIMITED', 0)}")
    print(f"V2_PROJECTED_INSUFFICIENT={counts.get('INSUFFICIENT', 0)}")
    print(f"V2_PROJECTED_USABLE_TOTAL={counts.get('HIGH', 0) + counts.get('MEDIUM', 0) + counts.get('LIMITED', 0)}")
    print(f"V2_PROJECTED_FULL_DISTRIBUTION={counts.get('HIGH', 0) + counts.get('MEDIUM', 0)}")
    print(f"SOURCE_SCRAPING_CLEAN={len(market_rows)}")
    print(f"SOURCE_PROCASA_NETWORK_CLEAN={len(network_rows)}")
    print(f"WITH_5_OR_MORE_COMPARABLES_BEFORE={before_with_5}")
    print(f"WITH_5_OR_MORE_COMPARABLES_AFTER={after_with_5}")
    print(f"STILL_INSUFFICIENT_AFTER={insufficient_after}")
    print(f"ELIGIBLE_PROPERTIES_RECALCULATED={len(analyses)}")
    raw_16469_before = next((raw for raw in raw_targets if str(raw.get("codigo")) == "16469"), {})
    before_16469_count = int((((raw_16469_before.get("analisis_comparables") or {}).get("mercado") or {}).get("n_selected")) or 0)
    print(f"COMPARABLES_16469_BEFORE={before_16469_count}")
    print(f"COMPARABLES_16469_AFTER={control_16469.get('selected_count')}")
    print(f"SURFACE_USED_16469={control_16469.get('surface_used_m2')}")
    print(f"COMPARABLE_CANDIDATES_16469={control_16469.get('candidate_count')}")
    print(f"COMPARABLE_MEDIAN_UF_M2_16469={control_16469.get('median_uf_m2')}")
    print(f"TARGET_UF_M2_16469={control_16469.get('target_uf_m2')}")
    print(f"COMPARABLE_BAR_16469_VISIBLE={'YES' if control_16469.get('bar_expected_visible') else 'NO'}")
    print(f"V2_REASON_COUNTS={json.dumps(dict(reason_counts), ensure_ascii=False)}")
    for tipo in sorted(by_type_total):
        label = tipo.upper()
        print(f"{label}_TOTAL={by_type_total[tipo]}")
        print(f"{label}_USABLE={by_type_usable[tipo]}")
    print(f"AUDIT_REPORT={report_path}")

    if not args.write:
        print("WRITE_PERFORMED=NO")
        client.close()
        return 0

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = _write_local_snapshot(raw_targets, timestamp)
    print(f"LOCAL_BEFORE_SNAPSHOT={snapshot_path}")
    write_stats = _write_analysis(db, targets, analyses)
    print("WRITE_PERFORMED=YES")
    print(f"WRITE_MATCHED={write_stats['matched']}")
    print(f"WRITE_MODIFIED={write_stats['modified']}")
    print(f"WRITE_UPSERTED={write_stats['upserted']}")

    validation = _post_write_audit(db, raw_targets, analyses)
    errors = list(validation["errors"])
    validation["errors"] = errors
    report_payload.update(
        {
            "read_only": False,
            "local_before_snapshot": str(snapshot_path),
            "write_stats": write_stats,
            "post_write_validation": validation,
            "temp_v1_collection_deleted": False,
        }
    )
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    quality = validation["quality_counts"]
    usable_total = sum(int(quality.get(level, 0)) for level in ("HIGH", "MEDIUM", "LIMITED"))
    usable_percent = round(100.0 * usable_total / validation["target_count"], 2) if validation["target_count"] else 0.0
    check_16469 = validation["16469"]
    print(f"TARGET_COUNT={validation['target_count']}")
    print(f"ANALISIS_V2_WRITTEN={validation['analysis_v2_written']}")
    print(f"UNIQUE_CODES={validation['unique_codes']}")
    print(f"QUALITY_HIGH={quality.get('HIGH', 0)}")
    print(f"QUALITY_MEDIUM={quality.get('MEDIUM', 0)}")
    print(f"QUALITY_LIMITED={quality.get('LIMITED', 0)}")
    print(f"QUALITY_INSUFFICIENT={quality.get('INSUFFICIENT', 0)}")
    print(f"USABLE_TOTAL={usable_total}")
    print(f"USABLE_PERCENT={usable_percent}")
    print(f"16469_SURFACE_M2={check_16469.get('surface_used_m2')}")
    print(f"16469_STRUCTURALLY_COMPLETE_CANDIDATES={check_16469.get('candidate_count')}")
    print(f"16469_SAME_OPERATION_TYPE_COMMUNE_POOL={check_16469.get('pool_candidate_count')}")
    print(f"16469_SELECTED_N={check_16469.get('n_selected')}")
    print(f"16469_MEDIAN_UF_M2={check_16469.get('median_uf_m2')}")
    print(f"16469_TARGET_UF_M2={check_16469.get('property_uf_m2')}")
    print(f"16469_COMPARABLE_BAR_EXPECTED={'YES' if check_16469.get('bar_expected_visible') else 'NO'}")
    print(f"BEFORE_AFTER_AUDIT_SAMPLE={validation['sample_size']}")
    print(f"BEFORE_AFTER_AUDIT_PASS={'YES' if validation['sample_pass'] and validation['all_documents_non_analysis_unchanged'] else 'NO'}")
    print(f"UNEXPECTED_FIELDS_MODIFIED={json.dumps(validation['unexpected_fields_modified'], ensure_ascii=False)}")
    print("TEMP_V1_COLLECTION_DELETED=NO")
    print("NEW_COLLECTIONS_CREATED=NO")
    print(f"SOURCE_COLLECTION_MODIFIED_ONLY_FIELD={PORTFOLIO_COLLECTION}.analisis_comparables")
    print("SOURCE_SCRAPING_MODIFIED=NO")
    print(f"TESTS={'PASS' if not errors else 'FAIL'}")
    print(f"ERRORS={json.dumps(errors, ensure_ascii=False)}")
    print(f"MIGRATION_REPORT={report_path}")
    client.close()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
