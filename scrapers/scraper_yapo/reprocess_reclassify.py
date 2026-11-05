"""Reclassify existing documents from local HTML without re-downloading."""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import load_env_file, AppConfig
load_env_file()
cfg = AppConfig()

from pymongo import MongoClient
from scraping_yapo_proxys import _parse_html_fast, _build_rule_based_details, build_mongo_document, html_validator, LOGGER

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

def main():
    client = MongoClient(cfg.mongo_uri)
    col = client[cfg.mongo_db][cfg.mongo_collection]

    total = col.count_documents({})
    print(f"[RECLASSIFY] Coleccion {cfg.mongo_db}.{cfg.mongo_collection}: {total} documentos")

    # Get all docs with html_path
    docs = list(col.find({"html_path": {"$exists": True, "$ne": ""}}))
    print(f"[RECLASSIFY] Documentos con html_path: {len(docs)}")

    # Count states before
    pipeline = [{"$group": {"_id": "$classification.final_state", "count": {"$sum": 1}}}]
    before = {d["_id"] or "NONE": d["count"] for d in col.aggregate(pipeline)}
    print("\nAntes:")
    for s in ["CORREDOR_SEGURO", "DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO", "AD_REMOVED"]:
        print(f"  {s}: {before.get(s, 0)}")

    updated = 0
    failed = 0
    avatar_found = 0
    candidates_found = 0
    strong_broker_hits = 0
    start_ts = time.time()

    for i, doc in enumerate(docs):
        listing_id = doc.get("listing_id", "?")
        html_path_str = doc.get("html_path", "")
        html_path = Path(html_path_str) if html_path_str else None

        if not html_path or not html_path.exists():
            LOGGER.warning("[RECLASSIFY] HTML no encontrado: %s -> %s", listing_id, html_path_str)
            failed += 1
            continue

        try:
            html = html_path.read_text(encoding="utf-8", errors="replace")
            parsed = _parse_html_fast(html, url=doc.get("url", ""))
            record = _build_rule_based_details(parsed)
            record.update({
                "downloaded": doc.get("downloaded", True),
                "fetch_source": "reprocess",
                "html_path": str(html_path),
            })
            mongo_doc = build_mongo_document(record)
            mongo_doc["run_id"] = doc.get("run_id", mongo_doc["run_id"])

            # Preserve original fields we don't want to overwrite
            mongo_doc["_id"] = doc["_id"]

            # Check if avatar alt was found
            avatar_alt = mongo_doc.get("seller_avatar_alt", "")
            if avatar_alt:
                avatar_found += 1

            # Check publisher_identity_candidates
            candidates = mongo_doc.get("publisher_identity_candidates", [])
            if candidates:
                candidates_found += 1
                # Log first time we see candidates for this listing
                if avatar_alt:
                    LOGGER.info("[PARSE] listing_id=%s candidates=%d avatar_alt=%r",
                                listing_id, len(candidates), avatar_alt)

            # Track strong broker rule hits
            cls = mongo_doc.get("classification", {})
            if cls.get("decision_source") and cls.get("decision_source") != "deepseek":
                strong_broker_hits += 1
                if cls.get("final_state") == "CORREDOR_SEGURO" and cls.get("ai_used") is False:
                    LOGGER.info("[STRONG_RULE] listing_id=%s source=%s pattern=%s candidate_value=%s",
                                listing_id,
                                cls.get("decision_source", ""),
                                cls.get("decision_pattern", ""),
                                cls.get("evidence", [""])[0] if cls.get("evidence") else "")

            # Check if new classification differs
            old_state = (doc.get("classification") or {}).get("final_state", "")
            new_state = cls.get("final_state", "")
            changed = " [CHANGED]" if old_state != new_state else ""

            col.replace_one({"_id": doc["_id"]}, mongo_doc)
            updated += 1

            if changed:
                LOGGER.info("[RULE] listing_id=%s %s -> %s evidence=%s decision_source=%s%s",
                            listing_id, old_state, new_state,
                            cls.get("evidence", []),
                            cls.get("decision_source", ""),
                            changed)

        except Exception as e:
            LOGGER.error("[RECLASSIFY_ERROR] listing_id=%s: %s", listing_id, e)
            failed += 1

        if (i + 1) % 50 == 0 or i == len(docs) - 1:
            elapsed = time.time() - start_ts
            rate = (i + 1) / (elapsed / 60) if elapsed > 0 else 0
            LOGGER.info("[RECLASSIFY] processed=%d/%d updated=%d failed=%d avatar_found=%d elapsed=%.0fs rate=%.1f/min",
                        i + 1, len(docs), updated, failed, avatar_found, elapsed, rate)

    elapsed = time.time() - start_ts
    print(f"\n[RECLASSIFY] Completado: {updated} actualizados, {failed} fallos")
    print(f"[RECLASSIFY] avatar_alt encontrados: {avatar_found}  |  publisher_identity_candidates: {candidates_found}  |  strong_rule_hits: {strong_broker_hits}")
    print(f"[RECLASSIFY] Tiempo total: {elapsed:.0f}s ({updated/(elapsed/60):.1f}/min)")

    # States after
    pipeline = [{"$group": {"_id": "$classification.final_state", "count": {"$sum": 1}}}]
    after = {d["_id"] or "NONE": d["count"] for d in col.aggregate(pipeline)}
    print("\nDespues:")
    for s in ["CORREDOR_SEGURO", "DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO", "AD_REMOVED"]:
        print(f"  {s}: {after.get(s, 0)}")

    # Export inciertos to report
    from datetime import datetime
    reports_dir = Path(__file__).resolve().parent / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"yapo_inciertos_reclasificados_avatar_alt_{timestamp}.json"

    inciertos = list(col.find({"classification.final_state": "INCIERTO"}))
    inciertos_data = []
    for d in inciertos:
        cls_d = d.get("classification", {})
        inciertos_data.append({
            "listing_id": d.get("listing_id"),
            "url": d.get("url"),
            "title": d.get("title"),
            "seller_name": d.get("seller_name"),
            "seller_avatar_alt": d.get("seller_avatar_alt"),
            "publisher_identity_candidates": d.get("publisher_identity_candidates", []),
            "classification": cls_d,
            "classification_debug": d.get("classification_debug", cls_d.get("classification_debug", {})),
            "comuna": d.get("comuna"),
            "precio_raw": d.get("precio_raw"),
        })

    report_data = {
        "metadata": {
            "total_docs": total,
            "processed": len(docs),
            "updated": updated,
            "failed": failed,
            "avatar_alt_found": avatar_found,
            "publisher_identity_candidates_found": candidates_found,
            "strong_broker_rule_hits": strong_broker_hits,
            "states_before": before,
            "states_after": after,
            "timestamp": timestamp,
        },
        "inciertos": inciertos_data,
    }

    report_path.write_text(json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[RECLASSIFY] Reporte inciertos exportado: {report_path}")
    print(f"[RECLASSIFY] Inciertos en coleccion: {len(inciertos_data)}")

    # Summary
    print(f"\n=== VEREDICTO RECLASIFICACION ===")
    print(f"  publisher_identity_candidates encontrados: {candidates_found}")
    print(f"  strong_broker_rule_hits: {strong_broker_hits}")
    print(f"  avatar_alt encontrados: {avatar_found}")

if __name__ == "__main__":
    main()
