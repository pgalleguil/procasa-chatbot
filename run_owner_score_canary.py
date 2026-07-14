"""Read-only owner-score canary. It never updates MongoDB."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from pymongo import MongoClient

from config import Config
from owner_scoring import (
    build_source_signal_snapshot,
    calculate_owner_score,
    compute_publisher_activity,
    property_fingerprint,
    propose_classification_state,
)


ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports" / "owner_score_canary_20260714"
CANARY_IDS = (
    "25867672", "4248794", "4092056", "25899684", "4249579",
    "25871243", "30727724", "32588092", "30355312", "4211158",
    "32405203", "25870526", "25872023", "25931918", "32142195",
    "32419703",
)
MANUAL_STATE = {
    "25867672": "DUEÑO_SEGURO",
    "4248794": "INCIERTO",
    "4092056": "INCIERTO",
    "25899684": "CORREDOR_SEGURO",
    "4249579": "INCIERTO",
    "25871243": "DUEÑO_SEGURO",
    "30727724": "INCIERTO",
    "32588092": "INCIERTO",
    "30355312": "INCIERTO",
    "4211158": "INCIERTO",
    "32405203": "CORREDOR_SEGURO",
    "25870526": "CORREDOR_SEGURO",
    "25872023": "CORREDOR_SEGURO",
    "25931918": "CORREDOR_SEGURO",
    "32142195": "CORREDOR_SEGURO",
    "32419703": "CORREDOR_SEGURO",
}
for _listing_id, _state in list(MANUAL_STATE.items()):
    if _state.startswith("DUE") and _state.endswith("O_SEGURO"):
        MANUAL_STATE[_listing_id] = "DUE\u00d1O_SEGURO"
MANUAL_NOTES = {
    "25867672": "Primera persona inequívoca: 'VENDO MI DEPARTAMENTO'.",
    "4248794": "La referencia a 'sus dueños' está en tercera persona.",
    "4092056": "'Trato directo con sus dueños' no identifica al publicador.",
    "25899684": "La descripción exige comisión de corretaje.",
    "4249579": "Particular sin declaración en primera persona; agendar visita es neutral.",
    "25871243": "Primera persona: 'Vendo mi departamento'; sin identidad comercial visible.",
    "30727724": "Identidad personal y tipo Propietario son señales débiles, no concluyentes.",
    "32588092": "Tipo Propietario sin declaración personal explícita.",
    "30355312": "Tipo Agente, pero sin empresa/badge concluyente; requiere incertidumbre.",
    "4211158": "No existen señales útiles: fallback neutral.",
    "32405203": "Grecop Corredores y seller_type Agente.",
    "25870526": "Marca Propiedades, badge profesional y Agente.",
    "25872023": "Identidad explícita de inmobiliaria.",
    "25931918": "Propiedades Santa María, badge profesional y Agente.",
    "32142195": "eXp Chile, badge profesional y Agente.",
    "32419703": "Grecop Corredores; 'un solo dueño' es referencia jurídica.",
}


def _load_yapo_parser():
    candidates = [
        ROOT / "scraping" / "scraping_yapo_proxys.py",
        Path(r"C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok - copia (2)\scraping\scraping_yapo_proxys.py"),
    ]
    path = next((candidate for candidate in candidates if candidate.exists()), None)
    if not path:
        return None, None
    sys.path[:0] = [str(path.parent.parent), str(path.parent)]
    spec = importlib.util.spec_from_file_location("yapo_canary_parser", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path.parent


def _enrich(doc: dict[str, Any], parser, parser_dir: Path | None) -> dict[str, Any]:
    data = dict(doc)
    html_path = str(doc.get("html_path") or "")
    if doc.get("origen") == "yapo" and parser and parser_dir and html_path:
        candidates = [
            Path(html_path),
            parser_dir / html_path,
            Path(r"C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok - copia (2)\scraping") / html_path,
        ]
        path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
        if path.exists():
            parsed = parser._parse_html_fast(path.read_text(encoding="utf-8", errors="replace")) or {}
            data.update(parsed)
    data["property_fingerprint"] = property_fingerprint(data)
    return data


def main() -> int:
    parser, parser_dir = _load_yapo_parser()
    db = MongoClient(Config.MONGO_URI)[Config.DB_NAME]
    collection = db[Config.CAPTACION_COLLECTION_NAME]
    docs = list(collection.find({"listing_id": {"$in": list(CANARY_IDS)}}))
    by_id = {str(doc.get("listing_id")): doc for doc in docs}
    missing = [listing_id for listing_id in CANARY_IDS if listing_id not in by_id]
    if missing:
        raise RuntimeError(f"Canary IDs missing: {missing}")

    cohort = [_enrich(by_id[listing_id], parser, parser_dir) for listing_id in CANARY_IDS]
    rows = []
    for data in cohort:
        activity = compute_publisher_activity(data, cohort, window_days=90)
        data["publisher_activity"] = activity
        result = calculate_owner_score(data)
        previous = data.get("classification", {}).get("state", "")
        if previous.startswith("DUE") and previous.endswith("O_SEGURO"):
            previous = "DUE\u00d1O_SEGURO"
        proposed = propose_classification_state(result)
        manual = MANUAL_STATE[str(data["listing_id"])]
        positive = [s for s in result.signals if s["weight"] > 0]
        negative = [s for s in result.signals if s["weight"] < 0]
        rows.append({
            "listing_id": str(data["listing_id"]),
            "previous_state": previous,
            "proposed_state": proposed,
            "technical_confidence": data.get("classification", {}).get("confidence"),
            "owner_score": result.score,
            "positive_signals": positive,
            "negative_signals": negative,
            "source_signals": build_source_signal_snapshot(data),
            "manual_conclusion": manual,
            "manual_notes": MANUAL_NOTES[str(data["listing_id"])],
            "proposed_matches_manual": proposed == manual,
            "previous_matches_manual": previous == manual,
        })

    acceptance = {
        "explicit_commercial_assigned": sum(
            row["manual_conclusion"] == "CORREDOR_SEGURO"
            and row["proposed_state"] != "CORREDOR_SEGURO" for row in rows
        ),
        "low_owner_without_explanation": sum(
            row["proposed_state"] == "DUEÑO_SEGURO" and row["owner_score"] < 70 for row in rows
        ),
        "uncertain_distinct_scores": sorted({
            row["owner_score"] for row in rows if row["proposed_state"] == "INCIERTO"
        }),
        "neutral_50_with_useful_signals": sum(
            row["owner_score"] == 50
            and bool(row["positive_signals"] or row["negative_signals"]) for row in rows
        ),
        "proposed_manual_errors": sum(not row["proposed_matches_manual"] for row in rows),
        "previous_system_errors": sum(not row["previous_matches_manual"] for row in rows),
    }
    acceptance["passed"] = (
        acceptance["explicit_commercial_assigned"] == 0
        and acceptance["low_owner_without_explanation"] == 0
        and len(acceptance["uncertain_distinct_scores"]) >= 3
        and acceptance["neutral_50_with_useful_signals"] == 0
        and acceptance["proposed_manual_errors"] == 0
    )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"write_mode": "READ_ONLY", "rows": rows, "acceptance": acceptance}
    (REPORT_DIR / "canary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    headers = [
        "listing_id", "estado anterior", "estado propuesto", "confianza técnica",
        "owner_score", "señales positivas", "señales negativas",
        "conclusión manual", "resultado anterior",
    ]
    lines = ["# Canary owner_score", "", "| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        pos = ", ".join(f"{s['code']}({s['weight']:+d})" for s in row["positive_signals"]) or "—"
        neg = ", ".join(f"{s['code']}({s['weight']:+d})" for s in row["negative_signals"]) or "—"
        lines.append("| " + " | ".join([
            row["listing_id"], row["previous_state"], row["proposed_state"],
            str(row["technical_confidence"]), str(row["owner_score"]), pos, neg,
            f"{row['manual_conclusion']}: {row['manual_notes']}",
            "COINCIDE" if row["previous_matches_manual"] else "ERROR",
        ]) + " |")
    lines.extend(["", "## Criterios de aceptación", "", "```json", json.dumps(acceptance, ensure_ascii=False, indent=2), "```", ""])
    (REPORT_DIR / "canary.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(acceptance, ensure_ascii=False, indent=2))
    print(REPORT_DIR / "canary.md")
    return 0 if acceptance["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
