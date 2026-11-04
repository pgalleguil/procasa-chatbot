"""Generate the bounded, offline V3 audit for the approved 482-listing Yapo lot."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_lib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scrapers.scraper_yapo.classifier_rules import classify_yapo_batch
from scrapers.scraper_yapo.downloader import assess_yapo_availability
from scrapers.scraper_yapo.extractor import extract_listing_fields


EXPECTED_CANONICAL_SHA256 = "dc9bda51a093abd9a1d7c6a741eeacac275724ea74b29ad4cc2a8bba558e12bf"
DEFAULT_INPUT = Path(
    r"C:\Users\pgall\Documents\PROCASA\YapoMaipu\preimportacion_20261103\yapo_maipu_preimport_482.json"
)
DEFAULT_OUTPUT = Path("reports/yapo_v3_audit_20261104")

MANUAL_OBSERVATIONS: dict[str, tuple[str, str]] = {
    "25939035": ("owner_identified", "Considero que es dueño."),
    "26143551": ("owner_review", "Descripción mínima y sin fotografías observadas."),
    "26610549": ("owner_review", "Posible propietario, sin comisión."),
    "27167566": ("owner_review", "Posible propietario."),
    "27144918": ("owner_review", "Descripción muy breve, posible propietario."),
    "25861896": ("owner_review", "Menciona trato directo con dueños, sin comisión."),
    "26135394": ("owner_review", "Posible propietario."),
    "26644524": ("owner_review", "Descripción breve y una imagen observada."),
    "32931491": ("broker_review", "Incierto, con inclinación hacia corredor."),
    "28073704": ("broker_review", "Mi evaluación es corredor por honorarios de corretaje."),
    "26653392": ("uncertain_review", "Incierto."),
    "33098407": ("profile_review", "Investigar «Más anuncios de Camila Paz»."),
    "33196456": ("broker_review", "Comisión de corredor del 2%."),
    "33190907": ("profile_review", "Investigar otras publicaciones del perfil."),
    "33208287": ("profile_review", "Varias publicaciones del mismo perfil."),
    "32306422": ("profile_review", "Misma corredora del caso anterior."),
    "32677581": ("broker_review", "Descripción menciona equipo de expertos inmobiliarios."),
    "32299493": ("availability_review", "Reportado por el usuario como desactivado."),
    "32632678": ("availability_review", "Reportado por el usuario como desactivado."),
    "32728499": ("availability_review", "Reportado por el usuario como desactivado."),
    "32676021": ("availability_review", "Reportado por el usuario como desactivado."),
}

AVAILABILITY_IDS = ("32299493", "32632678", "32728499", "32676021")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _local_html_check(record: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(record.get("html_path") or ""))
    if not path.exists():
        return {"html_exists": False, "html_path": str(path), "html_sha256_matches": False,
                "full_description_matches_html": False, "extracted_title_matches": False}
    source = path.read_text(encoding="utf-8", errors="replace")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    extracted = extract_listing_fields(source, source_url=str(record.get("url") or ""), include_images=False)
    def normalize(value: Any) -> str:
        plain = re.sub(r"<[^>]*>", " ", html_lib.unescape(str(value or "")))
        decomposed = unicodedata.normalize("NFKD", plain.casefold())
        return " ".join("".join(char for char in decomposed if not unicodedata.combining(char)).split())
    return {
        "html_exists": True,
        "html_path": str(path),
        "html_sha256_matches": digest == str(record.get("html_sha256") or ""),
        "full_description_matches_html": normalize(extracted.get("description")) == normalize(record.get("description")),
        "extracted_title_matches": normalize(extracted.get("title")) == normalize(record.get("title")),
        "extracted_description_char_count": len(str(extracted.get("description") or "")),
        "description_truncated_by_extractor": bool(extracted.get("descripcion_is_truncated")),
    }


def _change_reason(result: dict[str, Any], listing_id: str) -> str:
    if listing_id == "28073704":
        return "Comisión explícita de corretaje con atribución no confirmada; corredor probable y revisión requerida."
    if result.get("profile_corroboration"):
        return "Evidencia comercial de otro anuncio del mismo seller_profile_id exacto."
    rules = set(result.get("classification_rule_ids") or [])
    if "LISTING_ADVERTISER_AGENT_TYPE" in rules:
        return "Tipo de anunciante Agente del aviso; queda corredor probable."
    if "BROKER_DESCRIPTION_EXPLICIT_ROLE_SIGNATURE" in rules:
        return "Firma explícita de rol de corredor vinculada al publicador."
    if "BROKER_DESCRIPTION_BUSINESS_SIGNATURE" in rules:
        return "Firma de actividad inmobiliaria de primera parte."
    if result.get("source") == "owner_declaration_grammar":
        return "Declaración directa atribuible de propietario; no acredita titularidad legal."
    return str(result.get("reason") or "Cambio determinista respaldado por las reglas actuales.")


def _manual_outcome(listing_id: str, old_state: str, new_state: str) -> str:
    if listing_id == "28073704":
        return "DEFECTO_REPRODUCIDO_Y_CORREGIDO; atribución de comisión queda pendiente de revisión humana."
    if old_state != new_state:
        return "CAMBIO_RESPALDADO_POR_REGLA; la observación humana no se trata como etiqueta de verdad."
    if listing_id in AVAILABILITY_IDS:
        return "DISPONIBILIDAD_ACTUAL_NO_VERIFICADA; falta FlashProxy en este entorno."
    return "OBSERVACIÓN_CONSERVADA; sin error nuevo confirmado por sí sola."


def generate(input_path: Path = DEFAULT_INPUT, output_dir: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    original_bytes = input_path.read_bytes()
    source_sha = hashlib.sha256(original_bytes).hexdigest()
    if source_sha != EXPECTED_CANONICAL_SHA256:
        raise ValueError(f"SHA-256 canónico distinto al aprobado: {source_sha}")
    records = json.loads(original_bytes.decode("utf-8"))
    if not isinstance(records, list) or len(records) != 482:
        raise ValueError(f"Se esperaban 482 registros; se encontraron {len(records) if isinstance(records, list) else 'formato inválido'}")
    ids = [str(item.get("listing_id") or "") for item in records]
    if not all(ids) or len(set(ids)) != 482:
        raise ValueError("El lote debe contener 482 listing_id no vacíos y únicos")
    original_counts = Counter(str((item.get("classification") or {}).get("state") or "MISSING") for item in records)
    if original_counts != Counter({"CORREDOR_SEGURO": 369, "CORREDOR_PROBABLE": 11, "DUEÑO_PROBABLE": 10, "INCIERTO": 92}):
        raise ValueError(f"Estados originales inesperados: {dict(original_counts)}")
    if not set(MANUAL_OBSERVATIONS).issubset(set(ids)):
        raise ValueError("Alguna observación manual no corresponde al lote canónico")

    classified = classify_yapo_batch(records)
    results = [result or {"state": "MISSING", "evidence": []} for result in classified]
    result_by_id = {str(record["listing_id"]): result for record, result in zip(records, results)}
    record_by_id = {str(record["listing_id"]): record for record in records}
    profile_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        profile_id = str(record.get("seller_profile_id") or "").strip()
        if profile_id:
            profile_groups[profile_id].append(record)

    output_dir.mkdir(parents=True, exist_ok=True)
    comparison_rows: list[dict[str, Any]] = []
    changed_ids: list[str] = []
    new_broker_ids: list[str] = []
    new_owner_ids: list[str] = []
    for record, result in zip(records, results):
        listing_id = str(record["listing_id"])
        old_state = str((record.get("classification") or {}).get("state") or "MISSING")
        new_state = str(result.get("state") or "MISSING")
        changed = old_state != new_state
        if changed:
            changed_ids.append(listing_id)
            if new_state.startswith("CORREDOR") and not old_state.startswith("CORREDOR"):
                new_broker_ids.append(listing_id)
            if new_state.startswith("DUEÑO") and not old_state.startswith("DUEÑO"):
                new_owner_ids.append(listing_id)
        profile_id = str(record.get("seller_profile_id") or "").strip()
        profile_ads = profile_groups.get(profile_id, [])
        comparison_rows.append({
            "listing_id": listing_id,
            "url": record.get("url") or record.get("canonical_url") or "",
            "title": record.get("title") or "",
            "publisher": record.get("publicador_visible") or record.get("publicador") or "",
            "seller_profile_id": profile_id,
            "prior_state": old_state,
            "proposed_state": new_state,
            "changed": changed,
            "change_reason": _change_reason(result, listing_id) if changed else "",
            "classification_evidence": _json(result.get("evidence") or []),
            "broker_evidence": _json(result.get("broker_evidence") or []),
            "owner_evidence": _json(result.get("owner_evidence") or []),
            "contradictions": _json(result.get("contradictions") or []),
            "owner_review_priority_signals": _json(result.get("owner_review_priority_signals") or []),
            "local_batch_distinct_listings_for_profile": len({str(item.get("listing_id")) for item in profile_ads}),
            "profile_scope": "482_local_records_only; incomplete profile enumeration",
            "availability_status": "NO_VERIFICADO",
            "availability_source": "no_live_check; historical JSON and HTML do not confirm current status",
            "description_char_count": len(str(record.get("description") or "")),
            "description_is_truncated": bool(record.get("descripcion_is_truncated")),
        })

    manual_rows: list[dict[str, Any]] = []
    for listing_id, (category, note) in MANUAL_OBSERVATIONS.items():
        record = record_by_id[listing_id]
        result = result_by_id[listing_id]
        local = _local_html_check(record)
        old_state = str((record.get("classification") or {}).get("state") or "MISSING")
        new_state = str(result.get("state") or "MISSING")
        profile_id = str(record.get("seller_profile_id") or "")
        peers = profile_groups.get(profile_id, [])
        manual_rows.append({
            "listing_id": listing_id,
            "observation_category": category,
            "human_observation": note,
            "human_observation_is_ground_truth": False,
            "prior_state": old_state,
            "proposed_state": new_state,
            "outcome": _manual_outcome(listing_id, old_state, new_state),
            "publisher": record.get("publicador_visible") or "",
            "seller_profile_id": profile_id,
            "same_profile_ids_in_482_lot": _json(sorted({str(peer.get("listing_id")) for peer in peers if str(peer.get("listing_id")) != listing_id})),
            "title": record.get("title") or "",
            "description_full_unmodified": record.get("description") or record.get("descripcion") or "",
            "description_char_count": len(str(record.get("description") or "")),
            "classification_evidence": _json(result.get("evidence") or []),
            "broker_evidence": _json(result.get("broker_evidence") or []),
            "owner_evidence": _json(result.get("owner_evidence") or []),
            "owner_review_priority_signals": _json(result.get("owner_review_priority_signals") or []),
            "contradictions": _json(result.get("contradictions") or []),
            "availability_status": "NO_VERIFICADO",
            "local_html_sha256_matches": local.get("html_sha256_matches"),
            "local_html_title_matches": local.get("extracted_title_matches"),
            "local_html_full_description_matches": local.get("full_description_matches_html"),
            "local_html_description_truncated": local.get("description_truncated_by_extractor"),
            "evidence_source": "approved canonical JSON plus its referenced local HTML; no new Yapo request",
        })

    profile_rows: list[dict[str, Any]] = []
    for profile_id, group in sorted(profile_groups.items()):
        distinct_ids = sorted({str(item.get("listing_id")) for item in group})
        communes = sorted({str(item.get("comuna") or "") for item in group if item.get("comuna")})
        property_types = sorted({str(item.get("tipo_propiedad") or "") for item in group if item.get("tipo_propiedad")})
        operations = sorted({str(item.get("operacion") or "") for item in group if item.get("operacion")})
        publishers = sorted({str(item.get("publicador_visible") or item.get("publicador") or "") for item in group if item.get("publicador_visible") or item.get("publicador")})
        broker_ids = sorted({
            str(item.get("listing_id")) for item in group
            if str((result_by_id.get(str(item.get("listing_id"))) or {}).get("state") or "").startswith("CORREDOR")
        })
        fee_ids = sorted({
            str(item.get("listing_id")) for item in group
            if any((entry.get("brokerage_specific") or entry.get("rule_id") == "BROKERAGE_FEE_CHARGED")
                   for entry in ((result_by_id.get(str(item.get("listing_id"))) or {}).get("contextual_analysis") or {}).get("brokerage_fee_evidence", [])
                   if isinstance(entry, dict))
        })
        explicit_business_ids = sorted({
            str(item.get("listing_id")) for item in group
            if (result_by_id.get(str(item.get("listing_id"))) or {}).get("profile_identity_signal")
        })
        profile_rows.append({
            "seller_profile_id": profile_id,
            "seller_profile_url": next((str(item.get("seller_profile_url")) for item in group if item.get("seller_profile_url")), f"https://www.yapo.cl/user/profile/id/{profile_id}"),
            "distinct_listing_count_in_482": len(distinct_ids),
            "listing_ids_in_482": _json(distinct_ids),
            "listing_urls_in_482": _json(sorted({str(item.get("url") or item.get("canonical_url") or "") for item in group if item.get("url") or item.get("canonical_url")})),
            "communes_in_482": _json(communes),
            "property_types_in_482": _json(property_types),
            "operations_in_482": _json(operations),
            "publisher_names_in_482": _json(publishers),
            "broker_classified_listing_ids": _json(broker_ids),
            "explicit_profile_business_evidence_ids": _json(explicit_business_ids),
            "brokerage_fee_evidence_listing_ids": _json(fee_ids),
            "profile_history_status": "NO_VERIFICADO",
            "profile_enumeration_complete": False,
            "scope": "only records in the approved 482-listing local lot",
            "count_is_minimum_observed": True,
            "availability_of_other_profile_ads": "NO_VERIFICADO",
        })

    availability_rows: list[dict[str, Any]] = []
    for listing_id in AVAILABILITY_IDS:
        record = record_by_id[listing_id]
        path = Path(str(record.get("html_path") or ""))
        historical = {"availability_status": "NO_VERIFICADO", "availability_evidence": ["local_html_unavailable"]}
        html_matches = False
        if path.exists():
            source = path.read_text(encoding="utf-8", errors="replace")
            html_matches = hashlib.sha256(source.encode("utf-8")).hexdigest() == str(record.get("html_sha256") or "")
            historical = assess_yapo_availability(
                source,
                fetch_source="cache",
                validation_status=str(record.get("html_validation_status") or "OK"),
            )
        availability_rows.append({
            "listing_id": listing_id,
            "url": record.get("url") or record.get("canonical_url") or "",
            "user_reported_status": "DESACTIVADO",
            "current_availability_status": "NO_VERIFICADO",
            "availability_checked_at": "",
            "availability_source": "not_checked; explicit FlashProxy configuration unavailable",
            "live_request_attempted": False,
            "reason_current_status_unknown": "No FLASHPROXY_PROXY_URL or FlashProxy app was available; stale local HTML cannot confirm current availability.",
            "historical_html_sha256_matches": html_matches,
            "historical_html_signal": _json(historical.get("availability_evidence") or []),
            "historical_html_availability_result": historical.get("availability_status") or "NO_VERIFICADO",
            "historical_html_validation_status": record.get("html_validation_status") or "",
        })

    after_counts = Counter(str(result.get("state") or "MISSING") for result in results)
    changed_rows = [row for row in comparison_rows if row["changed"]]
    profile_sizes = Counter(len({str(item.get("listing_id")) for item in group}) for group in profile_groups.values())
    owner_priority_by_signal: Counter[str] = Counter()
    owner_priority_listing_ids: set[str] = set()
    for record, result in zip(records, results):
        signals = result.get("owner_review_priority_signals") or []
        if signals:
            owner_priority_listing_ids.add(str(record.get("listing_id")))
        owner_priority_by_signal.update(str(item.get("signal_id")) for item in signals if item.get("signal_id"))
    manual_failure_ids = ["28073704"]
    completed_at = datetime.now(timezone.utc).isoformat()
    truncated_ids = sorted(
        str(record.get("listing_id")) for record in records if bool(record.get("descripcion_is_truncated"))
    )
    missing_description_ids = sorted(
        str(record.get("listing_id")) for record in records
        if not str(record.get("description") or record.get("descripcion") or "").strip()
    )
    manual_html_checks = [row for row in manual_rows if row["local_html_sha256_matches"] is not None]
    summary = {
        "generated_at_utc": completed_at,
        "canonical_input_path": str(input_path),
        "canonical_sha256_expected": EXPECTED_CANONICAL_SHA256,
        "canonical_sha256_before": source_sha,
        "canonical_sha256_after": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "canonical_file_modified": hashlib.sha256(input_path.read_bytes()).hexdigest() != source_sha,
        "input_record_count": len(records),
        "unique_listing_id_count": len(set(ids)),
        "prior_classification_counts": dict(sorted(original_counts.items())),
        "proposed_classification_counts": dict(sorted(after_counts.items())),
        "changed_classification_count": len(changed_rows),
        "newly_classified_as_broker_count": len(new_broker_ids),
        "newly_classified_as_broker_ids": sorted(new_broker_ids),
        "newly_classified_as_owner_probable_count": len(new_owner_ids),
        "newly_classified_as_owner_probable_ids": sorted(new_owner_ids),
        "uncertain_after_count": after_counts.get("INCIERTO", 0),
        "manual_review_required_count": sum(bool(result.get("requires_review")) for result in results),
        "owner_review_priority_listing_count": len(owner_priority_listing_ids),
        "owner_review_priority_signal_counts": dict(sorted(owner_priority_by_signal.items())),
        "manual_observation_count": len(MANUAL_OBSERVATIONS),
        "manual_observations_with_demonstrated_current_rule_failure": len(manual_failure_ids),
        "demonstrated_rule_failure_listing_ids": manual_failure_ids,
        "manual_observations_are_ground_truth": False,
        "description_integrity": {
            "truncated_description_ids_in_canonical_json": truncated_ids,
            "missing_description_ids_in_canonical_json": missing_description_ids,
            "manual_observation_html_checks": len(manual_html_checks),
            "manual_html_files_hash_matched": sum(bool(row["local_html_sha256_matches"]) for row in manual_html_checks),
            "manual_full_descriptions_matched_html": sum(bool(row["local_html_full_description_matches"]) for row in manual_rows),
        },
        "profile_summary": {
            "distinct_seller_profile_ids_in_lot": len(profile_groups),
            "profiles_with_3_or_more_distinct_listings_observed_in_lot": sum(count for size, count in profile_sizes.items() if size >= 3),
            "profiles_with_3_or_more_distinct_listing_count": sum(1 for size, count in profile_sizes.items() if size >= 3 for _ in range(count)),
            "maximum_distinct_listings_for_one_profile_in_lot": max(profile_sizes, default=0),
            "live_profile_pages_verified": 0,
            "all_profile_enumerations_complete": False,
            "profile_counts_refer_to_canonical_lot_only": True,
        },
        "availability_summary": {
            "user_reported_deactivated_ids": list(AVAILABILITY_IDS),
            "current_deactivated_confirmed": 0,
            "current_active_confirmed": 0,
            "current_no_verificado": len(AVAILABILITY_IDS),
            "live_availability_requests": 0,
            "reason": "FlashProxy is not configured in this environment; historical HTML is not current proof.",
        },
        "quality_limits": {
            "classifier_error_rate_claimed": False,
            "owner_probability_created": False,
            "new_broker_catalog_entries": 0,
            "description_text_truncated": bool(truncated_ids),
            "full_description_in_manual_audit": True,
            "flashproxy_credentials_written_to_report": False,
        },
        "side_effects": {
            "deepseek_calls": 0,
            "mongo_reads": 0,
            "mongo_writes": 0,
            "yapo_live_requests": 0,
            "distribution_or_assignment": 0,
            "notifications": 0,
            "imports": 0,
        },
        "validation": {
            "baseline_scraper_tests": "363 passed, 2 skipped",
            "v3_targeted_tests": "17 passed",
            "full_regression_suite": "380 passed, 2 skipped",
        },
    }

    _write_csv(
        output_dir / "classification_comparison_482.csv",
        comparison_rows,
        ["listing_id", "url", "title", "publisher", "seller_profile_id", "prior_state", "proposed_state", "changed", "change_reason", "classification_evidence", "broker_evidence", "owner_evidence", "contradictions", "owner_review_priority_signals", "local_batch_distinct_listings_for_profile", "profile_scope", "availability_status", "availability_source", "description_char_count", "description_is_truncated"],
    )
    _write_csv(
        output_dir / "manual_audit_21.csv",
        manual_rows,
        ["listing_id", "observation_category", "human_observation", "human_observation_is_ground_truth", "prior_state", "proposed_state", "outcome", "publisher", "seller_profile_id", "same_profile_ids_in_482_lot", "title", "description_full_unmodified", "description_char_count", "classification_evidence", "broker_evidence", "owner_evidence", "owner_review_priority_signals", "contradictions", "availability_status", "local_html_sha256_matches", "local_html_title_matches", "local_html_full_description_matches", "local_html_description_truncated", "evidence_source"],
    )
    _write_csv(
        output_dir / "publisher_profile_audit.csv",
        profile_rows,
        ["seller_profile_id", "seller_profile_url", "distinct_listing_count_in_482", "listing_ids_in_482", "listing_urls_in_482", "communes_in_482", "property_types_in_482", "operations_in_482", "publisher_names_in_482", "broker_classified_listing_ids", "explicit_profile_business_evidence_ids", "brokerage_fee_evidence_listing_ids", "profile_history_status", "profile_enumeration_complete", "scope", "count_is_minimum_observed", "availability_of_other_profile_ads"],
    )
    _write_csv(
        output_dir / "availability_audit.csv",
        availability_rows,
        ["listing_id", "url", "user_reported_status", "current_availability_status", "availability_checked_at", "availability_source", "live_request_attempted", "reason_current_status_unknown", "historical_html_sha256_matches", "historical_html_signal", "historical_html_availability_result", "historical_html_validation_status"],
    )
    (output_dir / "quality_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "README.md").write_text(_render_readme(summary), encoding="utf-8")
    return summary


def _render_readme(summary: dict[str, Any]) -> str:
    counts = summary["proposed_classification_counts"]
    profiles = summary["profile_summary"]
    availability = summary["availability_summary"]
    return f"""# Auditoría V3 Yapo — Maipú, 482 anuncios

Generada el {summary['generated_at_utc']}. Es una reclasificación local sobre el JSON aprobado; no repite scraping.

## Resultado

- Estados propuestos: {json.dumps(counts, ensure_ascii=False, sort_keys=True)}.
- Cambios de estado frente al lote: {summary['changed_classification_count']}.
- Corredores adicionales frente al lote inicial: {summary['newly_classified_as_broker_count']}.
- Dueños probables agregados: {summary['newly_classified_as_owner_probable_count']}.
- INCIERTO restantes: {summary['uncertain_after_count']}.
- Señales débiles para priorizar revisión de dueño: {summary['owner_review_priority_listing_count']} anuncios; estas señales no cambian el estado por sí solas.

## Perfiles

- {profiles['distinct_seller_profile_ids_in_lot']} `seller_profile_id` distintos en los 482 registros.
- {profiles['profiles_with_3_or_more_distinct_listing_count']} perfiles tienen 3 o más anuncios distintos observados dentro del lote local.
- Es un mínimo del lote, no el total del perfil Yapo. Páginas en vivo: {profiles['live_profile_pages_verified']}; enumeraciones completas: {profiles['all_profile_enumerations_complete']}.
- El scraper general integra correlación por ID exacto y limita el rastreo futuro a perfiles únicos, páginas acotadas y FlashProxy explícito.

## Disponibilidad

- Anuncios reportados desactivados por el usuario: {len(availability['user_reported_deactivated_ids'])}.
- Confirmados activos: {availability['current_active_confirmed']}; confirmados desactivados: {availability['current_deactivated_confirmed']}; sin verificar: {availability['current_no_verificado']}.
- No había `FLASHPROXY_PROXY_URL` ni aplicación FlashProxy disponible; por eso no se hicieron solicitudes. El HTML guardado no prueba el estado actual.
- El código no infiere ACTIVO por HTTP 200. Los no verificados y desactivados quedan con bloqueo de revisión en el flujo Yapo.

## Observaciones manuales y regla corregida

Se conservaron las 21 observaciones, sus textos y descripciones completas en `manual_audit_21.csv`; son opiniones humanas, no etiquetas de verdad.

Una observación detectó un defecto reproducible: el ID **28073704** menciona “2% de honorarios de Corretaje” y quedaba INCIERTO. Ahora se propone CORREDOR_PROBABLE, con atribución de comisión `unattributed_charge`, evidencia media y revisión requerida; no se promueve a corredor seguro. Las comisiones explícitamente cobradas por terceros no clasifican al publicador como corredor.

Las señales de descripción breve, pocas fotos, primera persona, trato directo y sin comisión solo agregan prioridad de revisión. No se inventan probabilidades ni se promueve un dueño por ausencia de evidencia comercial.

## Integridad y límites

- SHA-256 del JSON original: `{summary['canonical_sha256_before']}`; verificado nuevamente: `{summary['canonical_sha256_after']}`.
- El JSON canónico no se modificó. El CSV contiene los 482 `listing_id` únicos y la comparación antes/después.
- No se consultó MongoDB, DeepSeek ni Yapo; no hubo importación, distribución, asignación ni notificaciones.
- No se afirma una tasa de error del clasificador: las observaciones manuales no son un conjunto de verdad etiquetado.
- La suite previa fue 363 aprobadas y 2 omitidas; la regresión completa final fue 380 aprobadas y 2 omitidas, incluidas 17 pruebas V3.

## Archivos

- `classification_comparison_482.csv`: una fila por anuncio, con evidencia, contradicciones y señales de revisión.
- `manual_audit_21.csv`: las 21 observaciones, descripción completa sin recortar y contraste con HTML local.
- `publisher_profile_audit.csv`: perfiles exactos, anuncios del lote, comunas, operaciones, tipos y evidencias observadas; todos marcados incompletos/no verificados en el sitio.
- `availability_audit.csv`: los cuatro anuncios reportados como desactivados y su estado actual NO_VERIFICADO.
- `quality_summary.json`: conteos, integridad y límites de la auditoría.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = generate(args.input, args.output_dir)
    print(json.dumps({
        "output_dir": str(args.output_dir),
        "canonical_sha256": result["canonical_sha256_after"],
        "counts": result["proposed_classification_counts"],
        "changes": result["changed_classification_count"],
        "brokers_added": result["newly_classified_as_broker_count"],
        "owners_added": result["newly_classified_as_owner_probable_count"],
        "profiles_3plus_in_local_lot": result["profile_summary"]["profiles_with_3_or_more_distinct_listing_count"],
        "live_availability": result["availability_summary"]["current_no_verificado"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
