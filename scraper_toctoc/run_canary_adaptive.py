"""Adaptive canary: 4 combos, multi-page, dedup, target 100 new candidates."""
import json, sys, time, os, subprocess, hashlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\scraper_toctoc')
from config import AppConfig
from mongo_store import MongoStore
from proxy_manager import ProxyManager

config = AppConfig()
RUN_ID = "canary_adaptive_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
OUT = config.reports_dir / "canary" / RUN_ID
OUT.mkdir(parents=True, exist_ok=True)

MIN_CANDIDATES = 100
MAX_CANDIDATES = 120
MAX_PAGES_PER_COMBO = 5
MAX_DISCOVERED_URLS = 600

SEARCHES = [
    ("venta", "departamento"),
    ("venta", "casa"),
    ("arriendo", "departamento"),
    ("arriendo", "casa"),
]

# ========= SNAPSHOT =========
print("=== SNAPSHOT BEFORE CANARY ===")
mongo = MongoStore(config)
col = mongo.collection()
pre_total_t = col.count_documents({"origen": "toctoc"})
pre_total_y = col.count_documents({"origen": "yapo"})
pre_states = {}
for s in col.aggregate([{"$match": {"origen": "toctoc"}}, {"$group": {"_id": "$classification.state", "count": {"$sum": 1}}}]):
    pre_states[s["_id"]] = s["count"]
pre_tids = set(d["listing_id"] for d in col.find({"origen": "toctoc"}, {"listing_id": 1, "_id": 0}))
pre_html = col.count_documents({"origen": "toctoc", "html_path": {"$nin": [None, ""]}})
pre_ds = col.count_documents({"origen": "toctoc", "classification.deepseek_status": {"$nin": [None, "", "LEGACY_UNKNOWN"]}})

snapshot = {
    "toctoc_total": pre_total_t, "yapo_total": pre_total_y,
    "toctoc_states": {str(k): v for k, v in pre_states.items()},
    "toctoc_ids": list(pre_tids),
    "with_html_path": pre_html, "with_deepseek_status": pre_ds,
}
json.dump(snapshot, open(OUT / "snapshot_before.json", "w"), ensure_ascii=False, indent=2)
print(f"Toctoc: {pre_total_t}, Yapo: {pre_total_y}")
print(f"States: {pre_states}")

# ========= CANARY =========
print("\n=== ADAPTIVE CANARY ===")
proxy_manager = ProxyManager.from_env()
if not proxy_manager.has_proxies():
    raise RuntimeError("No proxy configured.")

global_seen_ids = set(pre_tids)
candidates_processed = 0
mongo_inserts = 0
mongo_updates = 0
all_new_ids = []
search_stats = {}
per_page_metrics = []
total_deepseek_calls = 0
total_deepseek_errors = 0
total_fallbacks = 0
total_html_saved = 0
total_proxy_bytes = 0
total_download_failures = 0

total_start = time.time()

for idx, (op, tp) in enumerate(SEARCHES):
    if candidates_processed >= MAX_CANDIDATES:
        print(f"\nTarget reached ({candidates_processed} >= {MAX_CANDIDATES}). Stopping.")
        break
    
    label = f"{op}_{tp}"
    print(f"\n{'='*70}")
    print(f"[{idx+1}/{len(SEARCHES)}] {label}")
    print(f"{'='*70}")
    
    combo_start = time.time()
    combo_new = 0
    combo_page_metrics = []
    consecutive_empty_pages = 0
    
    for page in range(1, MAX_PAGES_PER_COMBO + 1):
        if candidates_processed >= MAX_CANDIDATES:
            print(f"  Target reached, stopping combo.")
            break
        
        if consecutive_empty_pages >= 2:
            print(f"  {consecutive_empty_pages} consecutive pages with 0 new, stopping combo.")
            break
        
        t0 = time.time()
        
        cmd = [
            sys.executable, str(Path(sys.path[0]) / "run_toctoc.py"),
            "run-full",
            "--operacion", op,
            "--tipo", tp,
            "--max-pages", "1",
            "--max-urls", "50",
            "--use-playwright-discovery",
            "--proxy-mode", "proxy",
            "--write-db",
        ]
        
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        t1 = time.time()
        duration = round(t1 - t0, 2)
        
        output = proc.stdout + proc.stderr
        
        # Find batch_id
        batch_id = ""
        for line in output.split("\n"):
            if "Batch:" in line:
                batch_id = line.split("Batch:")[-1].strip()
                break
        
        # Parse page metrics from output
        urls_found = 0
        urls_unique = 0
        prof_skipped = 0
        historical_dup = 0
        run_dup = 0
        download_ok = 0
        download_fail = 0
        ds_calls = 0
        ds_errors = 0
        fallbacks = 0
        
        for line in output.split("\n"):
            l = line.strip()
            if "Discovered" in l and "URLs" in l:
                try:
                    urls_found = int(l.split("Discovered")[-1].split("URLs")[0].strip())
                except: pass
            if "Pre-filter:" in l:
                parts = l.split()
                for i, p in enumerate(parts):
                    if "kept" in p:
                        try: urls_unique = int(parts[i-1])
                        except: pass
                    if "skipped" in p:
                        try: prof_skipped = int(parts[i-1])
                        except: pass
            if "Historical dedup:" in l:
                try:
                    historical_dup = int(l.split(":")[1].split("skipped")[0].strip())
                except: pass
            if "Descargadas" in l:
                try:
                    download_ok = int(l.split("/")[0].split()[-1])
                except: pass
            if "Failed to download" in l:
                download_fail += 1
        
        # Read processed file
        page_new = 0
        if batch_id:
            proc_path = config.reports_dir / f"processed_{batch_id}.json"
            if proc_path.exists():
                processed = json.load(open(proc_path, encoding="utf-8"))
                page_unique = 0
                page_hist = 0
                page_skip = 0
                for p in processed:
                    pid = str(p.get("listing_id", ""))
                    skip = p.get("skip_reason", "")
                    if skip == "PROFESSIONAL_URL_FORMAT":
                        page_skip += 1
                    elif skip == "HISTORICAL_DUPLICATE":
                        page_hist += 1
                    elif pid and pid not in global_seen_ids:
                        global_seen_ids.add(pid)
                        all_new_ids.append(pid)
                        page_unique += 1
                    elif pid and pid in global_seen_ids:
                        pass  # run duplicate
                
                page_new = page_unique
                candidates_processed += page_new
                combo_new += page_new
                
                if page_new == 0:
                    consecutive_empty_pages += 1
                else:
                    consecutive_empty_pages = 0
                
                # Estimate DeepSeek calls from processed records
                for p in processed:
                    ds = p.get("classification", {}).get("deepseek_status", "")
                    if ds and ds not in ("", "NOT_SET", "LEGACY_UNKNOWN"):
                        total_deepseek_calls += 1
                        if ds in ("INVALID_EMPTY_CONTENT", "INVALID_JSON", "API_ERROR", "TIMEOUT"):
                            total_deepseek_errors += 1
                    if p.get("classification", {}).get("source") == "rules_fallback" or \
                       "fallback" in str(p.get("classification", {}).get("reason", "")).lower():
                        total_fallbacks += 1
                    if p.get("html_path"):
                        total_html_saved += 1
                    if p.get("fetch_source") in ("failed", "playwright_error") or \
                       p.get("html_validation_status") in ("INVALID", "BLOCKED"):
                        total_download_failures += 1
        
        page_metric = {
            "operacion": op,
            "tipo": tp,
            "pagina": page,
            "urls_encontradas": urls_found,
            "urls_unicas": urls_unique,
            "profesionales_descartados": prof_skipped,
            "duplicados_historicos": historical_dup,
            "candidatos_nuevos": page_new,
            "tiempo_s": duration,
            "batch_id": batch_id or "",
        }
        per_page_metrics.append(page_metric)
        combo_page_metrics.append(page_metric)
        
        print(f"  Pagina {page}: encontradas={urls_found} unicas={urls_unique} prof_skip={prof_skipped} hist_dup={historical_dup} nuevos={page_new} ({duration}s)")
        
        if page_new > 0:
            print(f"    Acumulado combo: {combo_new}, global: {candidates_processed}")
    
    search_stats[label] = {
        "time_s": round(time.time() - combo_start, 2),
        "pages_completed": len(combo_page_metrics),
        "candidates_in_combo": combo_new,
        "consecutive_empty_pages": consecutive_empty_pages,
        "stop_reason": "target_reached" if candidates_processed >= MAX_CANDIDATES else (
            "consecutive_empty" if consecutive_empty_pages >= 2 else "max_pages"),
        "per_page_metrics": combo_page_metrics,
    }

total_time = time.time() - total_start
print(f"\n{'='*70}")
print(f"CANARY COMPLETE: {candidates_processed} new candidates in {round(total_time, 2)}s")
print(f"{'='*70}")

# ========= POST-RUN SNAPSHOT =========
print("\n=== POST-RUN SNAPSHOT ===")
post_total_t = col.count_documents({"origen": "toctoc"})
post_total_y = col.count_documents({"origen": "yapo"})
post_states = {}
for s in col.aggregate([{"$match": {"origen": "toctoc"}}, {"$group": {"_id": "$classification.state", "count": {"$sum": 1}}}]):
    post_states[s["_id"]] = s["count"]
post_html = col.count_documents({"origen": "toctoc", "html_path": {"$nin": [None, ""]}})
post_ds = col.count_documents({"origen": "toctoc", "classification.deepseek_status": {"$nin": [None, "", "LEGACY_UNKNOWN"]}})

print(f"Toctoc: {pre_total_t} -> {post_total_t} (+{post_total_t - pre_total_t})")
print(f"Yapo: {pre_total_y} -> {post_total_y} (should be unchanged)")
yapo_changed = post_total_y != pre_total_y
print(f"Yapo modified: {yapo_changed}")

# ========= NEW DUEÑO_SEGURO PREVIEW =========
print("\n=== NEW DOCS PREVIEW ===")
new_owners = []
new_uncertain = []
new_excluded = []

for d in col.find({"origen": "toctoc", "listing_id": {"$in": all_new_ids}},
                  {"_id": 0, "listing_id": 1, "url": 1, "comuna": 1, "title": 1,
                   "seller_name": 1, "seller_type": 1, "classification": 1}):
    state = d.get("classification", {}).get("state")
    rec = {
        "listing_id": d.get("listing_id"), "url": d.get("url"),
        "comuna": d.get("comuna"), "title": d.get("title"),
        "seller_name": d.get("seller_name"), "seller_type": d.get("seller_type"),
        "state": state,
        "confidence": d.get("classification", {}).get("confidence"),
        "reason": d.get("classification", {}).get("reason", ""),
        "deepseek_status": d.get("classification", {}).get("deepseek_status", "N/A"),
    }
    if state == "DUEÑO_SEGURO":
        new_owners.append(rec)
    elif state == "INCIERTO":
        new_uncertain.append(rec)
    else:
        new_excluded.append(rec)

json.dump(new_owners, open(OUT / "crm_duenos_seguros_nuevos.json", "w"), ensure_ascii=False, indent=2)
json.dump(new_uncertain, open(OUT / "crm_inciertos_nuevos_revision.json", "w"), ensure_ascii=False, indent=2)
print(f"New DUEÑO_SEGURO: {len(new_owners)}")
print(f"New INCIERTO: {len(new_uncertain)}")
print(f"New other: {len(new_excluded)}")

# ========= FINAL REPORT =========
report = {
    "run_id": RUN_ID,
    "duration_s": round(total_time, 2),
    "target_met": candidates_processed >= MIN_CANDIDATES,
    "target": MIN_CANDIDATES,
    "max_candidates": MAX_CANDIDATES,
    "new_unique_candidates_processed": candidates_processed,
    "searches_executed": len(SEARCHES),
    "total_discovered_urls": sum(m.get("urls_encontradas", 0) for m in per_page_metrics),
    "professional_skipped": sum(m.get("profesionales_descartados", 0) for m in per_page_metrics),
    "historical_duplicates": sum(m.get("duplicados_historicos", 0) for m in per_page_metrics),
    "download_failures": total_download_failures,
    "deepseek_calls": total_deepseek_calls,
    "deepseek_errors": total_deepseek_errors,
    "fallbacks_used": total_fallbacks,
    "html_files_saved": total_html_saved,
    "mongo_inserts": post_total_t - pre_total_t,
    "pre_snapshot": snapshot,
    "post_snapshot": {
        "toctoc_total": post_total_t, "yapo_total": post_total_y,
        "states": {str(k): v for k, v in post_states.items()},
        "with_html_path": post_html,
        "with_deepseek_status": post_ds,
        "yapo_changed": yapo_changed,
    },
    "crm_preview": {
        "new_owners": len(new_owners),
        "new_uncertain": len(new_uncertain),
        "new_excluded": len(new_excluded),
    },
    "search_stats": search_stats,
    "per_page_metrics": per_page_metrics,
}

json.dump(report, open(OUT / "canary_adaptive_report.json", "w"), ensure_ascii=False, indent=2)
print(f"\nReport saved to: {OUT / 'canary_adaptive_report.json'}")
print(f"CRM nuevos duenos: {OUT / 'crm_duenos_seguros_nuevos.json'}")
print(f"CRM nuevos inciertos: {OUT / 'crm_inciertos_nuevos_revision.json'}")
print("DONE")
