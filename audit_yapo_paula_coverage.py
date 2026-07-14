"""Run the real Yapo entrypoint once per Paula commune and report coverage."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

from pymongo import MongoClient

from config import Config

ROOT = Path(__file__).resolve().parent
COMMUNES = (
    "Talca", "San Clemente", "Linares", "Maule", "Pelarco", "Río Claro", "San Rafael",
    "Colbún", "Longaví", "San Javier", "Villa Alegre", "Yerbas Buenas", "Molina", "Curicó",
)
SEARCH_URLS = (
    "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades",
    "https://www.yapo.cl/searchresult/bienes-raices-alquiler-apartamentos",
    "https://www.yapo.cl/searchresult/bienes-raices-alquiler-casas",
)


def slug(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def listing_id(url: str) -> str:
    match = re.search(r"/(\d+)(?:[/?#]|$)", url)
    return match.group(1) if match else ""


def main() -> None:
    coll = MongoClient(Config.MONGO_URI)[Config.DB_NAME][Config.CAPTACION_COLLECTION_NAME]
    batch = "paula_coverage_all14_20260714"
    command = [sys.executable, str(ROOT / "scraper" / "run_owner_hunt.py"), "discover"]
    for url in SEARCH_URLS:
        command += ["--start-url", url]
    for commune in COMMUNES:
        command += ["--target-commune", commune]
    command += ["--max-pages", "5", "--max-urls", "1000", "--batch-id", batch, "--dry-run"]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, encoding="utf-8", errors="replace")
    report_path = ROOT / "scraper" / "reports" / f"discovered_{batch}.json"
    all_discovered = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else []
    page_lines = [line for line in result.stdout.splitlines() if line.startswith("Discovery page")]
    error_lines = [line for line in (result.stdout + "\n" + result.stderr).splitlines()
                   if "failed" in line.lower() or "error" in line.lower()]
    rows = []
    for commune in COMMUNES:
        discovered = [item for item in all_discovered if slug(item.get("discovery_comuna", "")) == slug(commune)]
        ids = [listing_id(item.get("url", "")) for item in discovered]
        existing = set()
        if ids:
            existing = {str(doc.get("listing_id")) for doc in coll.find({"listing_id": {"$in": ids}}, {"listing_id": 1})}
        row = {
            "commune": commune,
            "queries": list(SEARCH_URLS),
            "pages_visited": len(page_lines),
            "urls_discovered": len(discovered),
            "duplicates": sum(listing_id(item.get("url", "")) in existing for item in discovered),
            "new_publications": sum(listing_id(item.get("url", "")) not in existing for item in discovered),
            "errors": error_lines,
            "zero_reason": "" if discovered else (
                "download_or_validation_error" if error_lines
                else "no_matching_tiles_in_first_5_pages_of_each_search"
            ),
            "batch_id": batch,
            "entrypoint": "scraper/run_owner_hunt.py",
            "return_code": result.returncode,
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
    payload = {
        "communes_executed": len(COMMUNES), "all_14_executed": len(COMMUNES) == 14,
        "execution": {"entrypoint": "scraper/run_owner_hunt.py", "target_commune_arguments": list(COMMUNES),
                      "queries": list(SEARCH_URLS), "pages_visited_total": len(page_lines),
                      "return_code": result.returncode, "errors": error_lines},
        "rows": rows,
    }
    out = ROOT / "reports" / "paula_yapo_coverage_14_communes_20260714.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
