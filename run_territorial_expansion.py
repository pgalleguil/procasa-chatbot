"""
FULL TERRITORIAL EXPANSION: 37 communes, 6 agents, 4 combos.
Uses existing Toctoc pipeline: discovery -> download -> extract -> classify -> upsert.
Saves progress incrementally, can be resumed.
"""
import sys, os, re, time, json, hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from collections import defaultdict

ROOT = Path(__file__).resolve().parent
SCT = ROOT / "scraper_toctoc"
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(SCT))
from dotenv import load_dotenv; load_dotenv(dotenv_path=ROOT / ".env")

import requests as req_lib
from config import AppConfig
from mongo_store import MongoStore
from extractor import extract_listing_fields
from enrich import _enrich_property_fields
from discovery import discover_listing_urls
from classifier_rules import (
    classify_structural_broker, classify_structural_owner, classify_obvious_broker,
    should_invoke_deepseek, build_rule_context, detect_explicit_owner,
)
from pymongo import MongoClient

config = AppConfig()
MONGO_URI = os.getenv("MONGO_URI")
DB = os.getenv("MONGO_DB","yapo")
COL_NAME = os.getenv("CAPTACION_COLLECTION_NAME") or os.getenv("MONGO_COLLECTION","propiedades_captacion")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
col = client[DB][COL_NAME]
mongo = MongoStore(config)

BATCH_ID = f"toctoc_territorial_expansion_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
PROGRESS_FILE = SCT / f"reports/progress_{BATCH_ID}.json"

COMBOS = [
    ("venta", "departamento"),
    ("venta", "casa"),
    ("arriendo", "departamento"),
    ("arriendo", "casa"),
]

# AGENTS and their communes
def norm(s):
    if not s: return ""
    return str(s).lower().strip().replace("á","a").replace("é","e").replace("í","i").replace("ó","o").replace("ú","u").replace("ñ","n")

AGENT_COMMUNES = {}
for u in client[DB]["usuarios"].find({"rol": "agente", "active": {"$ne": False}}):
    comunas = u.get("comunas_interes", [])
    if isinstance(comunas, str): comunas = [c.strip() for c in comunas.split(",")]
    comunas = [c for c in comunas if c]
    nombre = u.get("nombre", "?")
    if nombre in ("Raquel Cheneaux", "Pablo Galleguillos", None) or not comunas:
        continue
    AGENT_COMMUNES[nombre] = {
        "email": u.get("email", ""),
        "comunas": comunas,
        "comunas_slug": [c.lower().replace(" ", "-").replace("á","a").replace("é","e").replace("í","i").replace("ó","o").replace("ú","u").replace("ñ","n") for c in comunas],
    }

# Build flat commune list with agents
COMMUNES_TO_PROCESS = []
for agent_name, agent in AGENT_COMMUNES.items():
    for i, comuna_orig in enumerate(agent["comunas"]):
        COMMUNES_TO_PROCESS.append({
            "comuna_human": comuna_orig,
            "comuna_slug": agent["comunas_slug"][i],
            "agent": agent_name,
            "email": agent["email"],
        })

# Remove duplicates (same commune for multiple agents — take first agent by load)
seen = set()
deduped = []
for c in COMMUNES_TO_PROCESS:
    if c["comuna_slug"] in seen: continue
    seen.add(c["comuna_slug"])
    deduped.append(c)

print(f"BATCH: {BATCH_ID}")
print(f"Agents: {len(AGENT_COMMUNES)}")
print(f"Unique communes to process: {len(deduped)}")
print(f"Combos per commune: {len(COMBOS)}")
print(f"Max searches: {len(deduped) * len(COMBOS)}")

# Load progress if resuming
if PROGRESS_FILE.exists():
    progress = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    completed = set(progress.get("completed_communes", []))
    stats = defaultdict(int, progress.get("stats", {}))
    print(f"Resuming: {len(completed)} communes already done")
else:
    completed = set()
    stats = defaultdict(int)

DRY_RUN = "--apply" not in sys.argv
print(f"Dry-run: {DRY_RUN}\n")

def save_progress():
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    json.dump({
        "batch_id": BATCH_ID,
        "completed_communes": list(completed),
        "stats": dict(stats),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, open(PROGRESS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

try:
    for i, commune in enumerate(deduped):
        comuna_slug = commune["comuna_slug"]
        comuna_human = commune["comuna_human"]
        
        if comuna_slug in completed:
            continue
        
        print(f"\n{'='*60}")
        print(f"  [{i+1}/{len(deduped)}] {comuna_human} ({comuna_slug}) — {commune['agent']}")
        print(f"{'='*60}")
        
        all_candidates = []
        
        for op, tipo in COMBOS:
            label = f"{op}/{tipo}"
            try:
                discovered = discover_listing_urls(
                    operacion=op, tipo=tipo, region="metropolitana",
                    comuna=comuna_slug, max_pages=5, max_urls=80,
                    batch_id=BATCH_ID, use_playwright=True,
                    publicador="2",
                )
            except Exception as e:
                print(f"  {label}: discovery error: {e}")
                continue
            
            if not discovered:
                continue
            
            # Filter: skip existing
            new_in_combo = 0
            for rec in discovered:
                lid = rec.get("listing_id", "")
                url = rec.get("url", "")
                if not url or not lid: continue
                exists = col.find_one({"origen": "toctoc", "listing_id": lid})
                if exists: continue
                all_candidates.append(rec)
                new_in_combo += 1
            
            print(f"  {label}: {len(discovered)} discovered, {new_in_combo} new")
            stats["discovered"] += len(discovered)
        
        stats["candidates"] += len(all_candidates)
        print(f"  Total new candidates: {len(all_candidates)}")
        
        if not all_candidates:
            completed.add(comuna_slug)
            save_progress()
            continue
        
        # Process each candidate (limit to 30 per commune)
        processed = 0
        for rec in all_candidates[:30]:
            lid = rec.get("listing_id", "")
            url = rec.get("url", "")
            if not url: continue
            
            parsed = urlparse(url)
            clean_url = urlunparse(parsed._replace(query="", fragment="")).rstrip("?&")
            
            # Download
            try:
                r = req_lib.get(url, headers={
                    "User-Agent": config.user_agent,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "es-CL,es;q=0.9",
                }, timeout=20)
                if r.status_code != 200 or len(r.text) < 500:
                    stats["dl_fail"] += 1; continue
                html = r.text; stats["dl_ok"] += 1
            except Exception:
                stats["dl_fail"] += 1; continue
            
            # Extract
            extracted = extract_listing_fields(html, url)
            desc = str(extracted.get("description") or extracted.get("descripcion") or "")
            title = str(extracted.get("title") or "")
            seller_type = str(extracted.get("seller_type") or "")
            if not desc and not title:
                stats["empty"] += 1; continue
            stats["ext_ok"] += 1
            
            extracted["body_text"] = ""
            pc = str(extracted.get("price") or "")
            m_uf = re.search(r'UF\s*([\d.]+(?:,\d+)?)', pc)
            m_clp = re.search(r'\$\s*([\d.]+)', pc)
            if m_uf: extracted["precio_uf"] = float(m_uf.group(1).replace(".","").replace(",","."))
            if m_clp: extracted["precio_clp"] = int(m_clp.group(1).replace(".",""))
            extracted["price"] = ""
            
            enriched = _enrich_property_fields(extracted, clean_url, config.uf_valor_clp, config.uf_fecha)
            
            # Classify
            classification = None
            br = classify_structural_broker(enriched)
            if br: classification = br
            if not classification:
                ow = classify_structural_owner(enriched)
                if ow: classification = ow
            if not classification:
                ob = classify_obvious_broker(enriched)
                if ob: classification = ob
            
            if not classification and len(desc) >= 20:
                should_ds, _ = should_invoke_deepseek(
                    "INCONCLUSIVE", desc, len(desc), False, seller_type,
                    has_strong_broker_rule=False, has_explicit_owner_rule=False
                )
                if should_ds and config.deepseek_enabled:
                    try:
                        from deepseek_classifier import classify_with_deepseek
                        rctx = build_rule_context(enriched)
                        ds = classify_with_deepseek(enriched, rctx, config)
                        stats["ds_calls"] += 1
                        if ds and ds.status == "VALID":
                            classification = {
                                "state": ds.state, "confidence": ds.confidence,
                                "reason": ds.reason, "evidence": ds.evidence,
                                "source": "deepseek", "deepseek_raw": ds.raw,
                                "deepseek_status": ds.status, "rule_state": "INCONCLUSIVE", "signals": {},
                            }
                            if ds.state == "DUEÑO_SEGURO" and not detect_explicit_owner(enriched):
                                classification.update(state="INCIERTO", confidence=0.6,
                                    reason="third-person", source="rules_fallback")
                    except Exception:
                        pass
            
            if not classification:
                classification = {
                    "state": "INCIERTO", "confidence": 0.55, "reason": "no rules",
                    "evidence": [], "source": "rules_fallback", "rule_state": "INCONCLUSIVE", "signals": {},
                }
            
            state = classification.get("state", "INCIERTO")
            try: cf = float(classification.get("confidence", 0) or 0)
            except: cf = 0.0
            if state == "INCIERTO" and (cf < 0.50 or cf >= 0.70): classification["confidence"] = 0.55
            elif state == "DUEÑO_PROBABLE" and cf < 0.70: classification["confidence"] = 0.75
            
            stats[f"state_{state}"] += 1
            
            now = datetime.now(timezone.utc).isoformat()
            raw_record = {
                **enriched, "classification": classification,
                "processed_at": now, "batch_id": BATCH_ID,
                "source": "owner_hunt", "origen": "toctoc", "source_portal": "toctoc",
                "schema_version": "crm_v1", "html_path": "", "fetch_source": "requests",
                "html_validation_status": "OK", "scrape_stage": "classification_done",
                "title_source": "detail_html", "url": url, "canonical_url": url,
                "description_length": len(desc), "description_is_truncated": False,
            }
            
            if not DRY_RUN:
                try:
                    mongo.upsert_listing(raw_record)
                    stats["persisted"] += 1
                    processed += 1
                except Exception as e:
                    stats["errors"] += 1
            else:
                stats["persisted"] += 1
                processed += 1
            
            time.sleep(0.1)
        
        print(f"  Processed: {processed} / {len(all_candidates[:30])}")
        completed.add(comuna_slug)
        save_progress()
        
        if (i+1) % 5 == 0:
            print(f"\n  --- Progress: {len(completed)}/{len(deduped)} communes, "
                  f"disc={stats['discovered']} dl={stats['dl_ok']} ext={stats['ext_ok']} "
                  f"persist={stats['persisted']} err={stats['errors']}")

except KeyboardInterrupt:
    print("\nInterrupted. Progress saved.")

# Final report
print(f"\n{'='*60}")
print(f"  FINAL REPORT — {BATCH_ID}")
print(f"{'='*60}")
print(f"  Communes processed: {len(completed)}/{len(deduped)}")
print(f"  Discovered: {stats['discovered']}")
print(f"  Downloaded: {stats['dl_ok']} ok / {stats['dl_fail']} fail")
print(f"  Extracted: {stats['ext_ok']}")
print(f"  Empty: {stats['empty']}")
print(f"  DeepSeek: {stats['ds_calls']}")
print(f"  Persisted: {stats['persisted']}")
print(f"  Errors: {stats['errors']}")
print(f"  States:")
for k in sorted(stats.keys()):
    if k.startswith("state_"):
        print(f"    {k.replace('state_','')}: {stats[k]}")

save_progress()
if not DRY_RUN and stats.get("persisted", 0) > 0:
    import subprocess
    _dist_script = ROOT / "scripts" / "run_distribution_after_scrape.py"
    if _dist_script.exists():
        try:
            _r = subprocess.run(
                [sys.executable, str(_dist_script)],
                capture_output=True, text=True, timeout=300,
            )
            if _r.stdout.strip():
                print(_r.stdout.strip())
            if _r.returncode != 0 and _r.stderr.strip():
                print(f"  [POST-SCRAPE] stderr: {_r.stderr.strip()[-400:]}")
        except Exception as _e:
            print(f"  [POST-SCRAPE] No se pudo distribuir: {_e}")
client.close()
