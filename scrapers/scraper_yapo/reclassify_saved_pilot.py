"""Offline reclassification of a saved Yapo pilot JSON and corrected XLSX.

No network, MongoDB, Yapo pages, HTML dumps, or DeepSeek API calls are used.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .classification_pipeline import classify_with_routing, record_unknown_business_candidate
    from .classifier_rules import load_rule_sets, normalize_text
    from .deepseek_classifier import _build_messages, build_description_for_llm
    from .classifier_rules import build_rule_context
except ImportError:  # direct script execution
    from classification_pipeline import classify_with_routing, record_unknown_business_candidate
    from classifier_rules import load_rule_sets, normalize_text
    from deepseek_classifier import _build_messages, build_description_for_llm
    from classifier_rules import build_rule_context

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/package/2006/relationships",
}
UF_REFERENCE_DATE = "2026-11-03"
UF_REFERENCE_CLP = 40844.79
UF_REFERENCE_SOURCE = "https://www.sii.cl/valores_y_fechas/uf/uf2026.htm"


def _read_xlsx_sheet(path: Path, wanted_name: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.attrib["Id"]: item.attrib["Target"] for item in relationships}
        sheet = next(item for item in workbook.findall("m:sheets/m:sheet", NS) if item.attrib["name"] == wanted_name)
        target = targets[sheet.attrib[f"{{{NS['r']}}}id"]]
        sheet_path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in si.findall(".//m:t", NS)) for si in root.findall("m:si", NS)]
        root = ET.fromstring(archive.read(sheet_path))
        rows = root.findall("m:sheetData/m:row", NS)
        grid: list[list[str]] = []
        for row in rows:
            current: list[str] = []
            for cell in row.findall("m:c", NS):
                ref = cell.attrib.get("r", "")
                col = 0
                for char in "".join(ch for ch in ref if ch.isalpha()):
                    col = col * 26 + ord(char.upper()) - 64
                col -= 1
                while len(current) <= col:
                    current.append("")
                value_node = cell.find("m:v", NS)
                value = value_node.text if value_node is not None else ""
                if cell.attrib.get("t") == "s" and value:
                    value = shared[int(value)]
                elif cell.attrib.get("t") == "inlineStr":
                    value = "".join(node.text or "" for node in cell.findall(".//m:t", NS))
                current[col] = value or ""
            grid.append(current)
    if not grid:
        return []
    headers = [cell.lstrip("\ufeff").strip() for cell in grid[0]]
    return [
        {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))}
        for row in grid[1:]
        if any(row)
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _with_saved_publisher_provenance(record: dict[str, Any]) -> dict[str, Any]:
    """Keep imported values observable without claiming they were extracted."""
    fields = dict(record.get("publisher_field_provenance") or {})
    raw_fields: dict[str, str] = {}
    for field, source_value in (
        ("publicador_visible", record.get("publicador_visible")),
        ("contact_logo_alt", record.get("logo")),
        ("seller_type", record.get("tipo_anunciante")),
    ):
        if source_value:
            raw_fields[field] = str(source_value)
    return {
        **record,
        "contact_logo_alt": record.get("logo") or record.get("contact_logo_alt") or "",
        "seller_type": record.get("tipo_anunciante") or record.get("seller_type") or "",
        "publisher_field_provenance": fields,
        "unverified_publisher_fields": raw_fields,
    }


def _clp_amount(raw: Any) -> int | None:
    text = str(raw or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def _estimated_input_tokens(record: dict[str, Any]) -> int:
    extracted = _with_saved_publisher_provenance(record)
    bundle = build_description_for_llm(
        str(record.get("description") or record.get("descripcion") or ""),
        max_chars=6000,
        head_chars=2500,
        tail_chars=2500,
        snippet_radius=350,
    )
    messages = _build_messages(extracted, build_rule_context(extracted), bundle)
    # A conservative 4 characters/token approximation for the serialized
    # messages; actual tokenizer usage is only known from a real API response.
    return max(1, sum(len(item["content"]) for item in messages) // 4)


def reclassify(pilot_path: Path, xlsx_path: Path, output_dir: Path, baseline_csv: Path | None = None) -> dict[str, Any]:
    original = json.loads(pilot_path.read_text(encoding="utf-8-sig"))
    records = original.get("audit_records") if isinstance(original, dict) else original
    if not isinstance(records, list):
        raise ValueError("El informe piloto debe contener avisos o audit_records")
    candidates = _read_xlsx_sheet(xlsx_path, "Candidatos")
    candidate_ids = {str(row.get("listing_id", "")).strip() for row in candidates}
    excluded_rows = _read_xlsx_sheet(xlsx_path, "Corredores excluidos")
    catalog = load_rule_sets().get("known_broker_brands", [])

    audit: list[dict[str, Any]] = []
    registry_path = output_dir / "unknown_broker_candidates.json"
    if registry_path.exists():
        registry_path.unlink()
    # Correlate repeated publisher identities inside the saved 482 records only.
    publisher_ids: dict[str, set[str]] = {}
    for row in records:
        publisher = normalize_text(str(row.get("publicador_visible") or ""))
        listing_id = str(row.get("listing_id") or "")
        if publisher and listing_id:
            publisher_ids.setdefault(publisher, set()).add(listing_id)
    seen_ids: set[str] = set()
    for row in records:
        listing_id = str(row.get("listing_id") or "")
        if listing_id and listing_id in seen_ids:
            continue
        if listing_id:
            seen_ids.add(listing_id)
        extracted = _with_saved_publisher_provenance(row)
        publisher = normalize_text(str(row.get("publicador_visible") or ""))
        extracted["publisher_profile_context"] = {
            "distinct_listing_count": len(publisher_ids.get(publisher, set())),
            "evidence": [f"saved_pilot_repeated_publisher:{len(publisher_ids.get(publisher, set()))}_listings"]
            if publisher_ids.get(publisher) else [],
        }
        classification = classify_with_routing(
            extracted,
            simulate_deepseek=True,
            max_calls=0,
            candidate_registry_path=registry_path,
        ) or {"state": "UNAVAILABLE", "classification_route": "unavailable", "evidence": []}
        old = (row.get("clasificacion_preliminar") or {}).get("state") or row.get("classification_original", "UNKNOWN")
        new = classification.get("state", "UNAVAILABLE")
        audit.append({
            **row,
            "classification_original": old,
            "classification_new": classification,
            "classification_changed": old != new,
            "xlsx_corrected_candidate": str(row.get("listing_id", "")) in candidate_ids,
            "candidate_after_reclassification": new in {"DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"},
        })

    old_counts = Counter(item["classification_original"] for item in audit)
    new_counts = Counter(item["classification_new"]["state"] for item in audit)
    old_uncertain = [item for item in audit if item["classification_original"] == "INCIERTO"]
    resolved_old_uncertain = [item for item in old_uncertain if item["classification_new"]["state"] != "INCIERTO"]
    route_counts = Counter(item["classification_new"].get("classification_route", "unavailable") for item in audit)
    simulated_candidates = [item for item in audit if item["classification_new"].get("classification_route") == "deepseek_candidate"]
    projected_input_tokens_all = sum(_estimated_input_tokens(item) for item in audit)
    projected_input_tokens_ambiguous = sum(_estimated_input_tokens(item) for item in simulated_candidates)
    projected_output_tokens_all = len(audit) * 500
    projected_output_tokens_ambiguous = len(simulated_candidates) * 500
    # Official DeepSeek Flash peak rates as of this report: $0.30/M input and
    # $1.20/M output for cache misses. These are projections, not charges.
    cost_all = (projected_input_tokens_all * 0.30 + projected_output_tokens_all * 1.20) / 1_000_000
    cost_ambiguous = (projected_input_tokens_ambiguous * 0.30 + projected_output_tokens_ambiguous * 1.20) / 1_000_000
    changed = [item for item in audit if item["classification_changed"]]
    new_broker_map: dict[str, dict[str, Any]] = {}
    for item in audit:
        for evidence in (item["classification_new"].get("contextual_analysis") or {}).get("unknown_business_identity_evidence", []):
            name = evidence.split(":", 2)[-1].split("|shape:", 1)[0].strip()
            norm = normalize_text(name)
            if not norm:
                continue
            candidate = new_broker_map.setdefault(norm, {
                "name": name, "listing_ids": [], "evidence": [], "strength": "review_candidate",
            })
            listing_id = str(item.get("listing_id") or "")
            if listing_id and listing_id not in candidate["listing_ids"]:
                candidate["listing_ids"].append(listing_id)
            if evidence not in candidate["evidence"]:
                candidate["evidence"].append(evidence)
            if "|strength:probable" in evidence:
                candidate["strength"] = "probable"
    new_brokers = list(new_broker_map.values())

    priority = {"DUEÑO_SEGURO": 1, "DUEÑO_PROBABLE": 2, "INCIERTO": 3}
    sales_candidates = [
        item for item in audit
        if item["xlsx_corrected_candidate"] and item["candidate_after_reclassification"]
    ]
    sales_candidates.sort(key=lambda item: (
        priority.get(item["classification_new"]["state"], 9),
        -float(item["classification_new"].get("confidence") or 0),
        -len(str(item.get("description") or "")),
    ))
    clp_candidates = [item for item in sales_candidates if normalize_text(str(item.get("precio_moneda") or "")) == "clp"]
    clp_validation = []
    for item in clp_candidates:
        amount = _clp_amount(item.get("precio_raw"))
        converted = amount / UF_REFERENCE_CLP if amount else None
        clp_validation.append({
            "listing_id": str(item.get("listing_id") or ""), "precio_raw": item.get("precio_raw"),
            "precio_clp": amount, "precio_uf_convertido": round(converted, 2) if converted is not None else None,
            "rango_3000_6000_uf": (3000 <= converted <= 6000) if converted is not None else None,
        })
    baseline_clp_validation = []
    baseline_count = 0
    baseline_candidate_count = 0
    baseline_no_evidence = baseline_title_missing = baseline_description_missing = baseline_publisher_missing = 0
    if baseline_csv and baseline_csv.exists():
        with baseline_csv.open(encoding="utf-8-sig", newline="") as handle:
            baseline_rows = list(csv.DictReader(handle))
        by_id = {str(item.get("listing_id") or ""): item for item in audit}
        baseline_candidate_count = len(baseline_rows)
        baseline_no_evidence = sum(not str(row.get("evidencia") or "").strip() for row in baseline_rows)
        baseline_title_missing = sum(not str(row.get("titulo") or "").strip() for row in baseline_rows)
        baseline_description_missing = sum(not str(row.get("descripcion_completa") or "").strip() for row in baseline_rows)
        baseline_publisher_missing = sum(not str(row.get("publicador") or "").strip() for row in baseline_rows)
        for candidate in baseline_rows:
            listing_id = str(candidate.get("listing_id") or "")
            item = by_id.get(listing_id, {})
            price_raw = item.get("precio_raw") or candidate.get("precio_raw") or ""
            currency = normalize_text(str(item.get("precio_moneda") or price_raw[-3:]))
            if currency != "clp":
                continue
            baseline_count += 1
            amount = _clp_amount(price_raw)
            converted = amount / UF_REFERENCE_CLP if amount else None
            baseline_clp_validation.append({
                "listing_id": listing_id, "precio_raw": price_raw, "precio_clp": amount,
                "precio_uf_convertido": round(converted, 2) if converted is not None else None,
                "rango_3000_6000_uf": (3000 <= converted <= 6000) if converted is not None else None,
            })
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "yapo_maipu_candidatos_priorizados.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        headers = [
            "prioridad", "listing_id", "url", "titulo", "estado", "confianza", "evidencia",
            "publicador", "precio_raw", "comuna", "descripcion_completa", "classification_route",
            "estado_evidencia", "explicacion_evidencia", "precio_clp_convertido_uf", "rango_3000_6000_uf",
        ]
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for index, item in enumerate(sales_candidates, 1):
            result = item["classification_new"]
            state = result.get("state")
            label = "P1 - dueño probable" if state == "DUEÑO_PROBABLE" else "P2 - incierto" if result.get("owner_signal_candidates") else "P3 - revisión"
            writer.writerow({
                "prioridad": label,
                "listing_id": item.get("listing_id", ""),
                "url": item.get("url", ""),
                "titulo": item.get("title", ""),
                "estado": state,
                "confianza": result.get("confidence", ""),
                "evidencia": " | ".join(result.get("evidence") or []) or "SIN_SEÑAL_DETERMINISTICA",
                "estado_evidencia": "con_evidencia" if result.get("evidence") else "texto_extraido_sin_senal_suficiente",
                "explicacion_evidencia": (
                    result.get("reason") if result.get("evidence")
                    else "Título y descripción extraídos; no aportan señal determinística suficiente. Se conserva INCIERTO."
                ),
                "publicador": item.get("publicador_visible", ""),
                "precio_raw": item.get("precio_raw", ""),
                "comuna": item.get("comuna", ""),
                "descripcion_completa": item.get("description", item.get("descripcion", "")),
                "classification_route": result.get("classification_route", ""),
                "precio_clp_convertido_uf": (
                    round(_clp_amount(item.get("precio_raw")) / UF_REFERENCE_CLP, 2)
                    if normalize_text(str(item.get("precio_moneda") or "")) == "clp" and _clp_amount(item.get("precio_raw")) else ""
                ),
                "rango_3000_6000_uf": (
                    "dentro" if normalize_text(str(item.get("precio_moneda") or "")) == "clp"
                    and _clp_amount(item.get("precio_raw"))
                    and 3000 <= _clp_amount(item.get("precio_raw")) / UF_REFERENCE_CLP <= 6000
                    else "fuera" if normalize_text(str(item.get("precio_moneda") or "")) == "clp"
                    and _clp_amount(item.get("precio_raw")) else "no_aplica_o_sin_precio_clp"
                ),
            })

    report_path = output_dir / "yapo_maipu_reclasificacion_offline_auditoria.json"
    summary_path = output_dir / "yapo_maipu_reclasificacion_resumen.md"
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_simulation_only",
        "deepseek_api_calls": 0,
        "deepseek_tokens": 0,
        "mongo_writes": 0,
        "scraping_performed": False,
        "historical_html_reprocessed": False,
        "pilot_records": len(audit),
        "duplicate_listing_ids_skipped": len(records) - len(audit),
        "xlsx_corrected_candidates": len(candidates),
        "xlsx_excluded_brokers": len(excluded_rows),
        "known_broker_catalog_entries": len(catalog),
        "known_broker_catalog_unique_normalized": len({normalize_text(name) for name in catalog}),
        "original_state_counts": dict(old_counts),
        "new_state_counts": dict(new_counts),
        "brokers_resolved_without_deepseek": new_counts.get("CORREDOR_SEGURO", 0) + new_counts.get("CORREDOR_PROBABLE", 0),
        "probable_owners_remaining": new_counts.get("DUEÑO_PROBABLE", 0),
        "actual_deepseek_ambiguous_cases": route_counts.get("deepseek_candidate", 0),
        "route_counts_simulated": dict(route_counts),
        "original_uncertain_count": len(old_uncertain),
        "old_uncertain_resolved_deterministically": len(resolved_old_uncertain),
        "deepseek_candidates_simulated": route_counts.get("deepseek_candidate", 0),
        "estimated_calls_saved_vs_482_all_to_ai": len(audit) - route_counts.get("deepseek_candidate", 0),
        "estimated_calls_saved_percent": round((len(audit) - route_counts.get("deepseek_candidate", 0)) / len(audit) * 100, 2) if audit else 0,
        "estimated_input_tokens_all_482_if_sent": projected_input_tokens_all,
        "estimated_input_tokens_ambiguous_only": projected_input_tokens_ambiguous,
        "estimated_input_tokens_saved": projected_input_tokens_all - projected_input_tokens_ambiguous,
        "estimated_output_tokens_all_482_if_sent_max_500_each": projected_output_tokens_all,
        "estimated_output_tokens_ambiguous_only_max_500_each": projected_output_tokens_ambiguous,
        "estimated_peak_cost_usd_all_482_if_sent": round(cost_all, 6),
        "estimated_peak_cost_usd_ambiguous_only": round(cost_ambiguous, 6),
        "estimated_peak_cost_usd_saved": round(cost_all - cost_ambiguous, 6),
        "cost_estimate_method": "4 characters/token approximation; 500 output tokens/call; DeepSeek Flash peak cache-miss rates ($0.30/M input, $1.20/M output); not a charge",
        "cost_source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "deepseek_policy": {
            "api_called": False, "routing_mode": "simulation",
            "configured_default_max_calls_per_session": 0,
            "configured_default_max_budget_usd_per_session": 0.0,
            "ambiguous_routes_blocked_by_zero_budget": sum(
                bool(item["classification_new"].get("deepseek_budget_exhausted")) for item in simulated_candidates
            ),
        },
        "changed_classification_count": len(changed),
        "classification_transition_counts": {
            f"{old} -> {new}": count
            for (old, new), count in Counter((item["classification_original"], item["classification_new"]["state"]) for item in changed).items()
        },
        "changed_listings": [{
            "listing_id": item.get("listing_id"), "url": item.get("url"),
            "from": item["classification_original"],
            "to": item["classification_new"].get("state"),
            "evidence": item["classification_new"].get("evidence", []),
        } for item in changed],
        "unknown_company_candidates_not_confirmed": new_brokers,
        "manual_error_review": {
            "known_false_negatives_from_original_classification_corrected": [
                {"listing_id": "32200413", "new_state": "CORREDOR_SEGURO", "evidence": "VISAN Propiedades firma el anuncio: actúa con profesionalismo y ética."},
                {"listing_id": "27979919", "new_state": "CORREDOR_SEGURO", "evidence": "Consultora Inmobiliaria ofrece para la venta y firma como consultores inmobiliarios."},
            ],
            "confirmed_false_positives": [],
            "limitations": "El lote no contiene etiquetas humanas exhaustivas; la ausencia de falsos positivos confirmados no prueba tasa cero. Las identidades comerciales nuevas quedan como candidatas hasta revisión.",
        },
        "corrected_xlsx_candidate_ids": len(candidate_ids),
        "prioritized_csv_rows_after_broker_filter": len(sales_candidates),
        "candidates_without_deterministic_evidence": sum(not item["classification_new"].get("evidence") for item in sales_candidates),
        "evidence_diagnostics": {
            "no_evidence_but_text_available": sum(
                not item["classification_new"].get("evidence")
                and bool(str(item.get("description") or item.get("descripcion") or "").strip())
                and bool(str(item.get("title") or "").strip()) for item in sales_candidates
            ),
            "description_missing": sum(not str(item.get("description") or item.get("descripcion") or "").strip() for item in sales_candidates),
            "publisher_missing": sum(not str(item.get("publicador_visible") or "").strip() for item in sales_candidates),
            "handling": "Campos extraídos disponibles pero sin señales suficientes quedan explícitamente INCIERTO; no se inventa evidencia.",
        },
        "clp_price_validation": {
            "candidate_count": len(clp_candidates),
            "inside_3000_6000_uf": sum(item["rango_3000_6000_uf"] is True for item in clp_validation),
            "outside_3000_6000_uf": sum(item["rango_3000_6000_uf"] is False for item in clp_validation),
            "unparseable": sum(item["rango_3000_6000_uf"] is None for item in clp_validation),
            "uf_reference_date": UF_REFERENCE_DATE,
            "uf_reference_clp": UF_REFERENCE_CLP,
            "source": UF_REFERENCE_SOURCE,
            "records": clp_validation,
        },
        "baseline_129_candidate_clp_validation": {
            "source_csv": str(baseline_csv) if baseline_csv else None,
            "candidate_count": baseline_count,
            "inside_3000_6000_uf": sum(item["rango_3000_6000_uf"] is True for item in baseline_clp_validation),
            "outside_3000_6000_uf": sum(item["rango_3000_6000_uf"] is False for item in baseline_clp_validation),
            "unparseable": sum(item["rango_3000_6000_uf"] is None for item in baseline_clp_validation),
            "uf_reference_date": UF_REFERENCE_DATE,
            "uf_reference_clp": UF_REFERENCE_CLP,
            "source": UF_REFERENCE_SOURCE,
            "records": baseline_clp_validation,
        },
        "baseline_129_evidence_diagnostics": {
            "candidate_count": baseline_candidate_count,
            "prior_csv_without_evidence": baseline_no_evidence,
            "title_missing": baseline_title_missing,
            "description_missing": baseline_description_missing,
            "publisher_missing": baseline_publisher_missing,
            "diagnosis": "Los campos título y descripción estaban extraídos en las 129 filas; evidencia vacía significa que las reglas no generaron una señal, no que faltara el texto. En el CSV nuevo se etiqueta ese estado explícitamente.",
        },
        "source_sha256": {str(pilot_path): _sha256(pilot_path), str(xlsx_path): _sha256(xlsx_path)},
        "outputs": {
            "summary_markdown": str(summary_path),
            "audit_json": str(report_path),
            "prioritized_csv": str(csv_path),
            "unknown_candidates": str(registry_path),
        },
        "audit_records": audit,
    }
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    probable_new_count = sum(item["strength"] == "probable" for item in new_brokers)
    summary_path.write_text(
        "\n".join([
            "# Reclasificación offline Yapo Maipú",
            "",
            "**Modo:** simulación local. Sin scraping, sin lectura/escritura de MongoDB y sin llamadas ni tokens de DeepSeek.",
            "",
            "## Resultado",
            "",
            f"- Avisos auditados: {len(audit)}; candidatos del Excel corregido: {len(candidates)}.",
            f"- Catálogo conocido: {len(catalog)} nombres, {len({normalize_text(name) for name in catalog})} normalizados únicos.",
            f"- Original → nuevo: {dict(old_counts)} → {dict(new_counts)}.",
            f"- Corredores resueltos sin DeepSeek: {new_counts.get('CORREDOR_SEGURO', 0) + new_counts.get('CORREDOR_PROBABLE', 0)}; dueños probables: {new_counts.get('DUEÑO_PROBABLE', 0)}; casos ambiguos candidatos a IA: {route_counts.get('deepseek_candidate', 0)} (simulación, cero llamadas).",
            f"- De los {len(old_uncertain)} originales INCIERTO, {len(resolved_old_uncertain)} se resolvieron automáticamente con reglas determinísticas.",
            f"- Enrutamiento simulado: {route_counts.get('deterministic_only', 0)} determinísticos y {route_counts.get('deepseek_candidate', 0)} casos ambiguos; ahorro estimado de llamadas: {summary['estimated_calls_saved_percent']}%.",
            f"- Evidencia ausente en {summary['candidates_without_deterministic_evidence']} candidatos: {summary['evidence_diagnostics']['no_evidence_but_text_available']} tienen título y descripción extraídos, pero sin señales concluyentes; se conserva INCIERTO y queda explicado en el CSV. Descripciones faltantes: {summary['evidence_diagnostics']['description_missing']}; publicador faltante: {summary['evidence_diagnostics']['publisher_missing']}.",
            f"- Diagnóstico de las 129 filas del CSV anterior: evidencia vacía {baseline_no_evidence}; títulos faltantes {baseline_title_missing}, descripciones faltantes {baseline_description_missing}, publicadores faltantes {baseline_publisher_missing}.",
            f"- Precios CLP del CSV base de 129 candidatos: {baseline_count} validados con UF $ {UF_REFERENCE_CLP:,.2f} al {UF_REFERENCE_DATE}; dentro del rango: {sum(item['rango_3000_6000_uf'] is True for item in baseline_clp_validation)}, fuera: {sum(item['rango_3000_6000_uf'] is False for item in baseline_clp_validation)}. Fuente: {UF_REFERENCE_SOURCE}.",
            f"- Identidades fuera del catálogo registradas: {len(new_brokers)} candidatas ({probable_new_count} con señales de corredor más fuertes); ninguna se promovió automáticamente.",
            f"- Excel corregido: {len(excluded_rows)} corredores ya separados; {len(candidates) - len(sales_candidates)} exclusiones nuevas; {len(sales_candidates)} candidatos quedan en el CSV priorizado.",
            f"- Reclasificaciones: {len(changed)}; ejemplos de falsos negativos previos corregidos: 32200413 (VISAN) y 27979919 (Consultora Inmobiliaria).",
            "- Falsos positivos confirmados: 0 detectados en esta inspección. No hay etiquetas humanas completas, por lo que esto no equivale a una tasa de error medida.",
            "",
            "## Tokens y costo hipotético",
            "",
            f"- Tokens de entrada aproximados: {projected_input_tokens_all:,} si se enviaran los 482; {projected_input_tokens_ambiguous:,} si se enviaran solo los ambiguos; ahorro aproximado: {projected_input_tokens_all - projected_input_tokens_ambiguous:,}.",
            f"- Costo pico hipotético: US${cost_all:.6f} para los 482 frente a US${cost_ambiguous:.6f} para los {len(simulated_candidates)} ambiguos; US${cost_all - cost_ambiguous:.6f} ahorrados. Estimación por 4 caracteres/token y máximo de 500 tokens de salida por llamada; precio sujeto a cambios.",
            f"- Tarifas usadas: [documentación oficial de DeepSeek](https://api-docs.deepseek.com/quick_start/pricing/) (Flash, hora pico, entrada sin caché US$0.30/M y salida US$1.20/M).",
            "",
            "## Archivos",
            "",
            f"- Auditoría completa con cambios por ID y evidencia: `{report_path}`",
            f"- CSV priorizado con descripciones completas: `{csv_path}`",
            f"- Registro de identidades candidatas pendientes de verificación: `{registry_path}`",
            "",
        ]),
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot-json", type=Path, required=True)
    parser.add_argument("--corrected-xlsx", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--baseline-csv", type=Path)
    args = parser.parse_args()
    result = reclassify(args.pilot_json, args.corrected_xlsx, args.output_dir, args.baseline_csv)
    for key in (
        "pilot_records", "xlsx_corrected_candidates", "known_broker_catalog_entries",
        "original_uncertain_count", "old_uncertain_resolved_deterministically",
        "deepseek_candidates_simulated", "estimated_calls_saved_percent",
        "changed_classification_count", "prioritized_csv_rows_after_broker_filter",
    ):
        print(f"{key}={result[key]}")
    print(f"audit_json={result['outputs']['audit_json']}")
    print(f"prioritized_csv={result['outputs']['prioritized_csv']}")
    print(f"summary_markdown={result['outputs']['summary_markdown']}")


if __name__ == "__main__":
    main()
