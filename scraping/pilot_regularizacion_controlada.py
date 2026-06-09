import asyncio
import hashlib
import os
from collections import Counter, defaultdict
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from scraping_yapo_proxys import (
    _parse_html_fast,
    classify_seller_state,
    clean_num,
    get_uf_value,
    parse_price_components,
    normalize_text,
)


ROOT = Path(__file__).resolve().parent
HTML_DIR = ROOT.parent / "html_dumps"


def _is_empty(v):
    return v is None or v == "" or v == "N/A" or v == [] or v == {}


def _md5_url(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest() + ".html"


def _resolve_precio_uf(price_text: str, existing_price_uf, price_clp_val, uf_val):
    if existing_price_uf not in (None, "", "N/A"):
        return existing_price_uf
    if price_clp_val and uf_val:
        resolved = round(price_clp_val / uf_val, 2)
        if resolved:
            return resolved
    p_uf, p_clp = parse_price_components(price_text or "")
    if p_uf is not None and uf_val:
        return round(p_uf, 2)
    if p_clp and uf_val:
        return round(p_clp / uf_val, 2)
    return None


async def main(limit: int = 20):
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]

    uf_value = await get_uf_value()
    print(f"UF usada para regularización: {uf_value:,.2f}")

    query = {
        "url": {"$exists": True, "$ne": None},
        "$or": [
            {"details.classification_state": {"$exists": False}},
            {"details.descripcion": {"$in": [None, "", "N/A"]}},
            {"details.enlaces_fotos": {"$in": [None, [], "N/A"]}},
            {"details.precio_uf": {"$in": [None, "", "N/A"]}},
        ],
    }

    total_candidates = await coll.count_documents(query)
    print(f"Candidatos históricos con vacíos + HTML local potencial: {total_candidates}")

    cursor = coll.find(query).sort("fecha_captura", -1).limit(limit)

    selected = []
    async for doc in cursor:
        url = doc.get("url")
        if not url:
            continue
        html_path = HTML_DIR / _md5_url(url)
        if html_path.exists():
            selected.append((doc, html_path))

    print(f"Muestra piloto seleccionada: {len(selected)} registros")

    before = Counter()
    after = Counter()
    updated = 0
    skipped = 0
    errors = 0
    details_changes = []

    for doc, html_path in selected:
        try:
            details = doc.get("details", {}) or {}
            url = doc.get("url")
            with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
                html = f.read()

            raw = _parse_html_fast(html)
            if not raw:
                skipped += 1
                continue

            publicador = raw.get("publicador", "N/A")
            company_name = raw.get("company_name", "N/A")
            seller_profile_id = raw.get("seller_profile_id", "N/A")
            seller_is_pro = raw.get("seller_is_pro", False)
            broker_brand = raw.get("broker_brand", "N/A")
            classification = classify_seller_state(
                publicador,
                raw.get("raw_desc", "N/A"),
                company_name,
                seller_profile_id,
                seller_is_pro,
                broker_brand,
                raw.get("multi_publisher_count"),
            )

            price_text = raw.get("price", "N/A")
            price_clp = clean_num(price_text)
            resolved_price_uf = _resolve_precio_uf(
                price_text,
                details.get("precio_uf"),
                price_clp,
                uf_value,
            )

            before.update({
                "descripcion_missing": 1 if _is_empty(details.get("descripcion")) else 0,
                "enlaces_missing": 1 if _is_empty(details.get("enlaces_fotos")) else 0,
                "precio_uf_missing": 1 if _is_empty(details.get("precio_uf")) else 0,
                "classification_missing": 1 if _is_empty(details.get("classification_state")) else 0,
                "score_dueno_missing": 1 if _is_empty(details.get("score_dueno")) else 0,
                "score_corredor_missing": 1 if _is_empty(details.get("score_corredor")) else 0,
            })

            update_fields = {}
            if _is_empty(details.get("descripcion")) and raw.get("raw_desc") not in (None, "", "N/A"):
                update_fields["details.descripcion"] = normalize_text(raw.get("raw_desc"), None)
            if _is_empty(details.get("enlaces_fotos")) and raw.get("images_url"):
                update_fields["details.enlaces_fotos"] = raw.get("images_url", [])
            if _is_empty(details.get("precio_uf")) and resolved_price_uf is not None:
                update_fields["details.precio_uf"] = resolved_price_uf
            if _is_empty(details.get("classification_state")) and classification.get("classification_state"):
                update_fields["details.classification_state"] = classification["classification_state"]
                update_fields["details.es_propietario_directo"] = classification["es_propietario_directo"]
                update_fields["details.es_corredor"] = classification["es_corredor"]
                update_fields["details.es_incierto"] = classification["es_incierto"]
            if _is_empty(details.get("score_dueno")) and classification.get("score_dueno") is not None:
                update_fields["details.score_dueno"] = classification["score_dueno"]
            if _is_empty(details.get("score_corredor")) and classification.get("score_corredor") is not None:
                update_fields["details.score_corredor"] = classification["score_corredor"]
            if _is_empty(details.get("motivos_dueno")) and classification.get("motivos_dueno") is not None:
                update_fields["details.motivos_dueno"] = classification["motivos_dueno"]
            if _is_empty(details.get("motivos_corredor")) and classification.get("motivos_corredor") is not None:
                update_fields["details.motivos_corredor"] = classification["motivos_corredor"]

            after.update({
                "descripcion_missing": 1 if _is_empty(update_fields.get("details.descripcion", details.get("descripcion"))) else 0,
                "enlaces_missing": 1 if _is_empty(update_fields.get("details.enlaces_fotos", details.get("enlaces_fotos"))) else 0,
                "precio_uf_missing": 1 if _is_empty(update_fields.get("details.precio_uf", details.get("precio_uf"))) else 0,
                "classification_missing": 1 if _is_empty(update_fields.get("details.classification_state", details.get("classification_state"))) else 0,
                "score_dueno_missing": 1 if _is_empty(update_fields.get("details.score_dueno", details.get("score_dueno"))) else 0,
                "score_corredor_missing": 1 if _is_empty(update_fields.get("details.score_corredor", details.get("score_corredor"))) else 0,
            })

            if update_fields:
                await coll.update_one(
                    {"_id": doc["_id"]},
                    {"$set": update_fields}
                )
                updated += 1
                details_changes.append((url, list(update_fields.keys())))
            else:
                skipped += 1

        except Exception as e:
            errors += 1
            print(f"ERROR en {doc.get('url', '')}: {e}")

    print("\n--- PILOTO REGULARIZACIÓN CONTROLADA ---")
    print(f"Procesados con HTML: {len(selected)}")
    print(f"Actualizados: {updated}")
    print(f"Saltados: {skipped}")
    print(f"Errores: {errors}")
    print("\nAntes:")
    print(f"  descripcion_missing={before['descripcion_missing']}")
    print(f"  enlaces_missing={before['enlaces_missing']}")
    print(f"  precio_uf_missing={before['precio_uf_missing']}")
    print(f"  classification_missing={before['classification_missing']}")
    print(f"  score_dueno_missing={before['score_dueno_missing']}")
    print(f"  score_corredor_missing={before['score_corredor_missing']}")
    print("\nDespués:")
    print(f"  descripcion_missing={after['descripcion_missing']}")
    print(f"  enlaces_missing={after['enlaces_missing']}")
    print(f"  precio_uf_missing={after['precio_uf_missing']}")
    print(f"  classification_missing={after['classification_missing']}")
    print(f"  score_dueno_missing={after['score_dueno_missing']}")
    print(f"  score_corredor_missing={after['score_corredor_missing']}")
    print("\nCambios aplicados (primeros 10):")
    for url, keys in details_changes[:10]:
        print(f"- {url} -> {', '.join(keys)}")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
