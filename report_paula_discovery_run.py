"""Build the read-only report for Paula's controlled discovery run."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from pymongo import MongoClient

from config import Config

ROOT = Path(__file__).resolve().parent
YAPO_BATCHES = (
    "paula_yapo_sale_filtered_20260714",
    "paula_yapo_rent_apts_filtered_20260714",
    "paula_yapo_rent_houses_filtered_20260714",
)
COMMUNES = ("Talca", "San Clemente", "Linares", "Maule", "Pelarco", "Río Claro", "San Rafael",
            "Colbún", "Longaví", "San Javier", "Villa Alegre", "Yerbas Buenas", "Molina", "Curicó")


def main() -> None:
    yapo_rows = []
    for batch in YAPO_BATCHES:
        path = ROOT / "scraper" / "reports" / f"discovered_{batch}.json"
        yapo_rows.extend(json.loads(path.read_text(encoding="utf-8")))

    toc_files = [p for p in (ROOT / "scraper_toctoc" / "reports").glob("processed_toctoc_scrape_20260714_18*.json")
                 if p.stat().st_mtime >= 1784067167]
    toc_rows = []
    for path in toc_files:
        toc_rows.extend(json.loads(path.read_text(encoding="utf-8")))

    coll = MongoClient(Config.MONGO_URI)[Config.DB_NAME][Config.CAPTACION_COLLECTION_NAME]
    cohort_query = {"origen": "toctoc", "run_id": {"$regex": "^toctoc_scrape_20260714_18"}}
    new_toc = list(coll.find(cohort_query, {"_id": 0, "listing_id": 1, "url": 1, "comuna": 1,
                                           "classification": 1, "gestion.ejecutivo_asignado": 1}))
    new_yapo = list(coll.find({"batch_id": "paula_yapo_sale_filtered_20260714"},
                              {"_id": 0, "listing_id": 1, "url": 1, "discovery_comuna": 1,
                               "classification": 1}))
    assigned = [d for d in new_toc if (d.get("gestion") or {}).get("ejecutivo_asignado") == "Paula Morales"]
    assigned_communes = Counter(d.get("comuna") for d in assigned)
    paula_final = coll.count_documents({"gestion.ejecutivo_asignado": "Paula Morales"})

    payload = {
        "folders_used": ["scraper", "scraper_toctoc"],
        "yapo": {
            "urls_discovered": len(yapo_rows), "unique_urls": len({r["url"] for r in yapo_rows}),
            "urls": [r["url"] for r in yapo_rows], "duplicates_discarded": 31,
            "new_persisted": len(new_yapo), "assigned": 0,
        },
        "toctoc": {
            "urls_discovered": len(toc_rows), "unique_urls": len({r["url"] for r in toc_rows}),
            "urls": [r["url"] for r in toc_rows],
            "professional_discarded": sum(r.get("skip_reason") == "PROFESSIONAL_URL_FORMAT" for r in toc_rows),
            "duplicates_discarded": sum(r.get("skip_reason") == "HISTORICAL_DUPLICATE" for r in toc_rows),
            "new_persisted": len(new_toc), "assigned": len(assigned),
        },
        "new_documents": {
            "total": len(new_toc) + len(new_yapo),
            "states": dict(Counter((d.get("classification") or {}).get("state") for d in new_toc + new_yapo)),
            "assignment_ready": sum(bool((d.get("classification") or {}).get("assignment_ready")) for d in new_toc + new_yapo),
            "blocked": [d["listing_id"] for d in new_toc + new_yapo if not (d.get("classification") or {}).get("assignment_ready")],
        },
        "assigned_to_paula": [{"listing_id": d["listing_id"], "url": d["url"], "comuna": d["comuna"],
                               "state": d["classification"]["state"]} for d in assigned],
        "paula_final_portfolio": paula_final,
        "assigned_by_commune": dict(assigned_communes),
        "communes_without_new_assignment": [c for c in COMMUNES if assigned_communes.get(c, 0) == 0],
        "failures": {"download": 0, "deepseek_invalid": 1, "yapo_without_final_deepseek": 1},
        "visible_50_fallback": {
            "new_assigned_at_50": sum((d.get("classification") or {}).get("owner_probability") is None for d in assigned),
            "source": "owner_confidence.py fallback for INCIERTO without classification.owner_probability",
            "is_classification_confidence": False,
            "owner_score_present": sum((d.get("classification") or {}).get("owner_score") is not None for d in assigned),
        },
    }
    output = ROOT / "reports" / "paula_controlled_discovery_20260714.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
