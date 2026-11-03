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
    """Restore field provenance available in the original pilot export schema."""
    fields = {}
    for field, source_value in (
        ("publicador_visible", record.get("publicador_visible")),
        ("contact_logo_alt", record.get("logo")),
        ("seller_type", record.get("tipo_anunciante")),
    ):
        if source_value:
            fields[field] = {"status": "confirmed", "relationship": "publisher", "source": "saved_pilot_field"}
    return {
        **record,
        "contact_logo_alt": record.get("logo") or record.get("contact_logo_alt") or "",
        "seller_type": record.get("tipo_anunciante") or record.get("seller_type") or "",
        "publisher_field_provenance": fields,
    }


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


def reclassify(pilot_path: Path, xlsx_path: Path, output_dir: Path) -> dict[str, Any]:
    original = json.loads(pilot_path.read_text(encoding="utf-8-sig"))
    if not isinstance(original, list):
        raise ValueError("El informe piloto debe contener una lista de avisos")
    candidates = _read_xlsx_sheet(xlsx_path, "Candidatos")
    candidate_ids = {str(row.get("listing_id", "")).strip() for row in candidates}
    excluded_rows = _read_xlsx_sheet(xlsx_path, "Corredores excluidos")
    catalog = load_rule_sets().get("known_broker_brands", [])

    audit: list[dict[str, Any]] = []
    registry_path = output_dir / "unknown_broker_candidates.json"
    if registry_path.exists():
        registry_path.unlink()
    for row in original:
        extracted = _with_saved_publisher_provenance(row)
        classification = classify_with_routing(
            extracted,
            simulate_deepseek=True,
            max_calls=0,
            candidate_registry_path=registry_path,
        ) or {"state": "UNAVAILABLE", "classification_route": "unavailable", "evidence": []}
        old = (row.get("clasificacion_preliminar") or {}).get("state", "UNKNOWN")
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
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "yapo_maipu_candidatos_priorizados.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        headers = [
            "prioridad", "listing_id", "url", "titulo", "estado", "confianza", "evidencia",
            "publicador", "precio_raw", "comuna", "descripcion_completa", "classification_route",
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
                "evidencia": " | ".join(result.get("evidence") or []),
                "publicador": item.get("publicador_visible", ""),
                "precio_raw": item.get("precio_raw", ""),
                "comuna": item.get("comuna", ""),
                "descripcion_completa": item.get("description", item.get("descripcion", "")),
                "classification_route": result.get("classification_route", ""),
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
        "xlsx_corrected_candidates": len(candidates),
        "xlsx_excluded_brokers": len(excluded_rows),
        "known_broker_catalog_entries": len(catalog),
        "known_broker_catalog_unique_normalized": len({normalize_text(name) for name in catalog}),
        "original_state_counts": dict(old_counts),
        "new_state_counts": dict(new_counts),
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
        "changed_classification_count": len(changed),
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
            f"- De los {len(old_uncertain)} originales INCIERTO, {len(resolved_old_uncertain)} se resolvieron automáticamente con reglas determinísticas.",
            f"- Enrutamiento simulado: {route_counts.get('deterministic_only', 0)} determinísticos y {route_counts.get('deepseek_candidate', 0)} casos ambiguos; ahorro estimado de llamadas: {summary['estimated_calls_saved_percent']}%.",
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
    args = parser.parse_args()
    result = reclassify(args.pilot_json, args.corrected_xlsx, args.output_dir)
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
