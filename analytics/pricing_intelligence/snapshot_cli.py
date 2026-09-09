"""Dry-run CLI for the V1 analytical snapshot foundation."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from bson.codec_options import CodecOptions
from pymongo import MongoClient

from config import Config

from .lead_linkage import LeadLinkageService
from .property_identity import build_property_identity_resolver, normalize_identifier
from .snapshot_builder import PropertySnapshotBuilder
from .snapshot_repository import PROPOSED_COLLECTION, PROPOSED_IDEMPOTENT_INDEX
from .time_utils import BUSINESS_TZ, UTC, is_in_previous_window, to_utc


PROPERTY_PROJECTION = {
    "_id": 0,
    "codigo": 1,
    "tipo_operacion": 1,
    "ubicacion.region": 1,
    "ubicacion.comuna": 1,
    "ubicacion.sector": 1,
    "caracteristicas.dormitorios": 1,
    "caracteristicas.banos": 1,
    "caracteristicas.estacionamientos": 1,
    "caracteristicas.superficie_construida": 1,
    "caracteristicas.superficie_terreno": 1,
    "metadata.tipo_propiedad": 1,
    "metadata.tipo_propiedad_detectado": 1,
    "publicaciones": 1,
    "historial_cambios": 1,
}
LEAD_PROJECTION = {
    "_id": 1,
    "created_at": 1,
    "prospecto.codigo": 1,
    "prospecto.codigo_mercadolibre": 1,
    "prospecto.codigo_yapo": 1,
    "prospecto.origen": 1,
    "prospecto.fuente_lead": 1,
    "prospecto.portal_origen": 1,
    "prospecto.origen_anuncio": 1,
    "prospecto.plataforma_origen": 1,
    "prospecto.codigo_propiedad": 1,
    "prospecto.propiedad_codigo": 1,
    "prospecto.codigo_referencia": 1,
    "prospecto.codigo_externo": 1,
    "prospecto.codigo_externo_aportado": 1,
    "prospecto.codigo_interno": 1,
    "prospecto.codigo_interno_procasa": 1,
    "prospecto.codigo_procasa": 1,
    "prospecto.external_id_origen": 1,
    "prospecto.codigo_consultado": 1,
    "prospecto.codigo_detectado": 1,
    "prospecto.codigo_encontrado": 1,
    "prospecto.codigo_interes": 1,
    "prospecto.codigo_mencionado": 1,
    "prospecto.codigo_propiedad_aludida": 1,
    "prospecto.codigo_propiedad_interes": 1,
    "prospecto.codigo_propiedad_interno": 1,
    "prospecto.codigo_sugerido": 1,
    "prospecto.url": 1,
    "prospecto.url_yapo": 1,
    "prospecto.link_yapo": 1,
    "prospecto.link_mercadolibre": 1,
    "prospecto.link_mercado_libre": 1,
    "prospecto.link_portal": 1,
    "prospecto.link_pendiente": 1,
    "prospecto.link_detectado": 1,
    "prospecto.link_externo": 1,
    "prospecto.ultimo_link_visto": 1,
    "prospecto.enlace": 1,
    "prospecto.enlace_ml": 1,
    "prospecto.enlace_externo": 1,
    "prospecto.enlace_portalinmobiliario": 1,
    "prospecto.propiedad_link": 1,
    "prospecto.origen_link": 1,
    "prospecto.debug_link": 1,
}
HISTORY_PROJECTION = {
    "_id": 0,
    "codigo": 1,
    "campo": 1,
    "fecha": 1,
    "valor_anterior": 1,
    "valor_nuevo": 1,
}
IDENTITY_PROJECTION = {
    "_id": 0,
    "codigo": 1,
    "publicaciones": 1,
}


def _percentage(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 2) if denominator else 0.0


def _coverage(snapshots: Iterable[Any]) -> dict[str, Any]:
    snapshots = list(snapshots)
    total = len(snapshots)
    fields = {
        "price_uf": sum(item.current_price_uf is not None for item in snapshots),
        "price_clp": sum(item.current_price_clp is not None for item in snapshots),
        "bedrooms": sum(item.bedrooms is not None for item in snapshots),
        "bathrooms": sum(item.bathrooms is not None for item in snapshots),
        "built_area_m2": sum(item.built_area_m2 is not None for item in snapshots),
        "land_area_m2": sum(item.land_area_m2 is not None for item in snapshots),
        "price_history": sum(item.data_quality.price_history_available for item in snapshots),
        "publication_data": sum(item.data_quality.publication_data_available for item in snapshots),
    }
    return {
        name: {"count": count, "pct": _percentage(count, total)} for name, count in fields.items()
    }


def _publication_counts(snapshots: Iterable[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for snapshot in snapshots:
        for publication in snapshot.publications:
            counts[publication.portal] += 1
    return dict(sorted(counts.items()))


def _exact_link_count(metrics: Mapping[str, Any]) -> int:
    counts = metrics.get("counts_by_status", {})
    return int(counts.get("EXACT_CANONICAL", 0)) + int(counts.get("EXACT_ALIAS", 0))


def _eligible_lead_counts(records: Iterable[Any], as_of: datetime) -> dict[str, int]:
    records = list(records)
    return {
        "7d": sum(
            record.created_at is not None
            and is_in_previous_window(record.created_at, as_of, 7)
            for record in records
            if record.status.value in {"EXACT_CANONICAL", "EXACT_ALIAS"}
        ),
        "30d": sum(
            record.created_at is not None
            and is_in_previous_window(record.created_at, as_of, 30)
            for record in records
            if record.status.value in {"EXACT_CANONICAL", "EXACT_ALIAS"}
        ),
    }


def _build_report(
    *,
    as_of: datetime,
    properties: list[Mapping[str, Any]],
    snapshots: Iterable[Any],
    errors: Iterable[str],
    leads: Iterable[Mapping[str, Any]],
    v1_service: LeadLinkageService,
    v1_records: Iterable[Any],
    v2_service: LeadLinkageService,
    v2_records: Iterable[Any],
    legacy_code_sets: Mapping[str, set[str]],
) -> dict[str, Any]:
    snapshots = list(snapshots)
    leads = list(leads)
    v1_records = list(v1_records)
    v2_records = list(v2_records)
    errors = list(errors)
    v2_metrics = v2_service.metrics(v2_records)
    v1_metrics = v1_service.metrics(v1_records)
    conflict_candidates = {
        candidate
        for record in v2_records
        if record.status.value == "CONFLICT"
        for candidate in record.resolution.candidates
    }
    ambiguous_candidates = {
        candidate
        for record in v2_records
        if record.status.value == "AMBIGUOUS"
        for candidate in record.resolution.candidates
    }
    v1_exact = _exact_link_count(v1_metrics)
    v2_exact = _exact_link_count(v2_metrics)
    gain = v2_exact - v1_exact
    return {
        "snapshot_date_local": as_of.astimezone(BUSINESS_TZ).date().isoformat(),
        "cutoff_utc": to_utc(as_of).isoformat(),
        "properties_processed": len(properties),
        "snapshots_built": len(snapshots),
        "errors": {"count": len(errors), "types": sorted(set(errors))},
        "linkage": dict(v2_metrics),
        "linkage_v1": dict(v1_metrics),
        "linkage_v2": dict(v2_metrics),
        "coverage_gain_vs_v1": {
            "absolute_leads": gain,
            "percentage_points": round(gain / len(v2_records) * 100, 2) if v2_records else 0.0,
            "relative_to_total": round(gain / len(v2_records), 6) if v2_records else 0.0,
        },
        "unmatched_reason_distribution_v1": v1_service.unmatched_reason_distribution(
            leads, legacy_code_sets
        ),
        "unmatched_reason_distribution_v2": v2_service.unmatched_reason_distribution(
            leads, legacy_code_sets
        ),
        "unmatched_diagnostic_dimensions_v1": v1_service.unmatched_diagnostic_dimensions(leads),
        "coverage": _coverage(snapshots),
        "publication_states_by_portal": _publication_counts(snapshots),
        "linked_leads_previous_7d": {
            "properties_with_signal": sum(item.linked_leads_previous_7d > 0 for item in snapshots),
            "total_leads": sum(item.linked_leads_previous_7d for item in snapshots),
        },
        "linked_leads_previous_30d": {
            "properties_with_signal": sum(item.linked_leads_previous_30d > 0 for item in snapshots),
            "total_leads": sum(item.linked_leads_previous_30d for item in snapshots),
        },
        "properties_without_linked_leads": sum(
            not item.data_quality.lead_linkage_available for item in snapshots
        ),
        "properties_with_identity_conflict": len(conflict_candidates),
        "properties_with_ambiguous_alias": len(ambiguous_candidates),
        "target_input_impact": {
            "v1": {
                "exact_linked_leads": v1_exact,
                "distinct_linked_properties": v1_metrics["distinct_linked_properties"],
                "eligible_leads_previous_7d": _eligible_lead_counts(v1_records, as_of)["7d"],
                "eligible_leads_previous_30d": _eligible_lead_counts(v1_records, as_of)["30d"],
            },
            "v2": {
                "exact_linked_leads": v2_exact,
                "distinct_linked_properties": v2_metrics["distinct_linked_properties"],
                "eligible_leads_previous_7d": _eligible_lead_counts(v2_records, as_of)["7d"],
                "eligible_leads_previous_30d": _eligible_lead_counts(v2_records, as_of)["30d"],
            },
        },
        "external_listing_views": "NOT_AVAILABLE_V1",
        "persistence": {
            "mongo_writes": 0,
            "collection_proposed": PROPOSED_COLLECTION,
            "future_index_documented_only": list(PROPOSED_IDEMPOTENT_INDEX),
        },
    }


def _load_sources(args: argparse.Namespace, as_of: datetime):
    if not Config.MONGO_URI:
        raise RuntimeError("MONGO_URI no está disponible en el entorno de ejecución")

    client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000)
    client.admin.command("ping")
    codec_options = CodecOptions(tz_aware=True, tzinfo=UTC)
    db = client.get_database(Config.DB_NAME, codec_options=codec_options)
    property_collection = db[getattr(Config, "PROPERTY_COLLECTION_NAME", "universo_cartera_prop360")]

    query: dict[str, Any] = {}
    requested_code = normalize_identifier(args.property_code) if args.property_code else None
    if requested_code:
        query = {"codigo": requested_code}
    cursor = property_collection.find(query, PROPERTY_PROJECTION).sort("codigo", 1)
    if args.sample is not None:
        cursor = cursor.limit(args.sample)
    properties = list(cursor)

    lead_cursor = db["leads"].find({}, LEAD_PROJECTION).batch_size(250)
    leads = list(lead_cursor)
    # A sample limits snapshot construction, not identity coverage.  The
    # resolver therefore uses the complete canonical/alias index while the
    # builder only materializes the requested sample.
    identity_properties = properties
    if args.sample is not None or requested_code:
        identity_properties = list(property_collection.find({}, IDENTITY_PROJECTION).sort("codigo", 1))
    v1_resolver = build_property_identity_resolver(
        identity_properties,
        enable_contextual_aliases=False,
    )
    v1_service = LeadLinkageService(v1_resolver)
    v1_records = v1_service.link_leads(leads)
    v2_resolver = build_property_identity_resolver(identity_properties, enable_contextual_aliases=True)
    v2_service = LeadLinkageService(v2_resolver)
    v2_records = v2_service.link_leads(leads)

    history_query: dict[str, Any] = {}
    property_codes = [normalize_identifier(item.get("codigo")) for item in properties]
    property_codes = [code for code in property_codes if code]
    if property_codes and (args.sample is not None or requested_code):
        history_query = {"codigo": {"$in": property_codes}}
    history = list(
        db["universo_cartera_prop360_historial"]
        .find(history_query, HISTORY_PROJECTION)
        .batch_size(250)
    )

    legacy_code_sets: dict[str, set[str]] = {}
    for collection_name in ("universo_cartera", "universo_obelix", "ingresos_supervisados"):
        legacy_code_sets[collection_name] = {
            code
            for code in (
                normalize_identifier(document.get("codigo"))
                for document in db[collection_name].find({}, {"_id": 0, "codigo": 1}).batch_size(250)
            )
            if code
        }

    builder = PropertySnapshotBuilder(
        v2_records,
        source_collection=getattr(Config, "PROPERTY_COLLECTION_NAME", "universo_cartera_prop360"),
        now_fn=lambda: as_of,
    )
    result = builder.build(
        properties,
        as_of=as_of,
        separate_price_events=history,
        property_code=requested_code,
    )
    return client, properties, leads, v1_service, v1_records, v2_service, v2_records, legacy_code_sets, result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pricing Intelligence V1 read-only dry-run")
    parser.add_argument("--dry-run", action="store_true", help="único modo permitido en V1")
    parser.add_argument("--sample", type=int, default=None, help="limita la muestra de propiedades")
    parser.add_argument("--property-code", default=None, help="procesa una propiedad exacta")
    parser.add_argument("--report-json", default=None, help="ruta local para reporte JSON sin PII")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.dry_run:
        print(
            "ABORTADO: pricing_intelligence V1 solo permite --dry-run; "
            "la persistencia Mongo todavía está deshabilitada.",
            file=sys.stderr,
        )
        return 2
    if args.sample is not None and args.sample <= 0:
        parser.error("--sample debe ser mayor que cero")

    as_of = datetime.now(BUSINESS_TZ)
    client = None
    try:
        (
            client,
            properties,
            leads,
            v1_service,
            v1_records,
            v2_service,
            v2_records,
            legacy_code_sets,
            result,
        ) = _load_sources(args, as_of)
        report = _build_report(
            as_of=as_of,
            properties=properties,
            snapshots=result.snapshots,
            errors=result.errors,
            leads=leads,
            v1_service=v1_service,
            v1_records=v1_records,
            v2_service=v2_service,
            v2_records=v2_records,
            legacy_code_sets=legacy_code_sets,
        )
        if args.report_json:
            output_path = Path(args.report_json)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"DRY-RUN ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
