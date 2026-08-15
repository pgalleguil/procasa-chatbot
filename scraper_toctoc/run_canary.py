"""Canary orchestrator: 12 searches, global dedup, 100-150 candidates, MongoDB write."""
import json, sys, time, os, subprocess, hashlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\scraper_toctoc')
from config import AppConfig
from mongo_store import MongoStore
from proxy_manager import ProxyManager

config = AppConfig()
RUN_ID = "canary_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
OUT = config.reports_dir / "canary" / RUN_ID
OUT.mkdir(parents=True, exist_ok=True)

MIN_CANDIDATES = 100
MAX_CANDIDATES = 150

# 12 searches
SEARCHES = [
    ("venta", "departamento", "la-florida"),
    ("venta", "casa", "la-florida"),
    ("arriendo", "departamento", "la-florida"),
    ("arriendo", "casa", "la-florida"),
    ("venta", "departamento", "santiago"),
    ("venta", "casa", "santiago"),
    ("arriendo", "departamento", "santiago"),
    ("venta", "departamento", "nunoa"),
    ("arriendo", "departamento", "nunoa"),
    ("arriendo", "casa", "nunoa"),
    ("venta", "departamento", "las-condes"),
    ("arriendo", "departamento", "las-condes"),
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
pre_html = col.count_documents({"origen": "toctoc", "html_dump_path": {"$nin": [None, ""]}})
pre_ds = col.count_documents({"origen": "toctoc", "classification.deepseek_status": {"$nin": [None, "", "LEGACY_UNKNOWN"]}})

snapshot = {
    "toctoc_total": pre_total_t, "yapo_total": pre_total_y,
    "toctoc_states": pre_states, "toctoc_ids": list(pre_tids),
    "with_html_path": pre_html, "with_deepseek_status": pre_ds,
}
json.dump(snapshot, open(OUT / "snapshot_before.json", "w"), ensure_ascii=False, indent=2)
print("Toctoc: {}, Yapo: {}".format(pre_total_t, pre_total_y))
print("States: {}".format(pre_states))
print("With html_path: {}, with deepseek_status: {}".format(pre_html, pre_ds))

# ========= CANARY =========
print("\n=== CANARY EXECUTION ===")
proxy_manager = ProxyManager.from_env()
if not proxy_manager.has_proxies():
    raise RuntimeError("No proxy configured.")

global_seen_ids = set(pre_tids)  # Dedup against existing
candidates_processed = 0
all_new_ids = []
search_stats = {}
total_start = time.time()

for idx, (op, tp, com) in enumerate(SEARCHES):
    if candidates_processed >= MAX_CANDIDATES:
        print("\nTarget reached ({} candidates). Stopping.".format(MAX_CANDIDATES))
        break
    
    label = "{}_{}_{}".format(op, tp, com)
    print("\n--- [{}/{}] {} ---".format(idx + 1, len(SEARCHES), label))
    
    t0 = time.time()
    
    # Build command
    cmd = [
        sys.executable, str(Path(sys.path[0]) / "run_toctoc.py"),
        "run-full",
        "--operacion", op,
        "--tipo", tp,
        "--comuna", com,
        "--estado", "2",
        "--publicador", "2",
        "--max-pages", "1",
        "--max-urls", "50",
        "--use-playwright-discovery",
        "--proxy-mode", "proxy",
        "--write-db",
    ]
    
    # Run via subprocess
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    t1 = time.time()
    
    # Parse output for candidate count
    output = proc.stdout + proc.stderr
    print(output[:500])
    
    # Find the batch_id from output
    batch_id = ""
    for line in output.split("\n"):
        if "Batch:" in line:
            batch_id = line.split("Batch:")[-1].strip()
            break
    
    # Read the processed file to count new candidates
    if batch_id:
        proc_path = config.reports_dir / "processed_{}.json".format(batch_id)
        if proc_path.exists():
            processed = json.load(open(proc_path, encoding="utf-8"))
            new_in_batch = 0
            for p in processed:
                pid = str(p.get("listing_id", ""))
                if pid and pid not in global_seen_ids:
                    global_seen_ids.add(pid)
                    all_new_ids.append(pid)
                    new_in_batch += 1
            candidates_processed += new_in_batch
            print("  Batch new: {}, cumulative: {}".format(new_in_batch, candidates_processed))
        else:
            print("  Processed file not found: {}".format(proc_path))
    
    search_stats[label] = {"time_s": round(t1 - t0, 2), "candidates_in_batch": new_in_batch if 'new_in_batch' in dir() else 0}

total_time = time.time() - total_start
print("\n" + "=" * 70)
print("CANARY COMPLETE: {} candidates processed in {}s".format(candidates_processed, round(total_time, 2)))

# ========= POST-RUN SNAPSHOT =========
print("\n=== POST-RUN SNAPSHOT ===")
post_total_t = col.count_documents({"origen": "toctoc"})
post_total_y = col.count_documents({"origen": "yapo"})
post_states = {}
for s in col.aggregate([{"$match": {"origen": "toctoc"}}, {"$group": {"_id": "$classification.state", "count": {"$sum": 1}}}]):
    post_states[s["_id"]] = s["count"]
post_html = col.count_documents({"origen": "toctoc", "html_dump_path": {"$nin": [None, ""]}})
post_ds = col.count_documents({"origen": "toctoc", "classification.deepseek_status": {"$nin": [None, "", "LEGACY_UNKNOWN"]}})

print("Toctoc: {} -> {} (+{})".format(pre_total_t, post_total_t, post_total_t - pre_total_t))
print("Yapo: {} -> {} (should be unchanged)".format(pre_total_y, post_total_y))
print("States: {}".format(post_states))
print("With html_path: {} (+{})".format(post_html, post_html - pre_html))
print("With deepseek_status: {} (+{})".format(post_ds, post_ds - pre_ds))
yapo_changed = post_total_y != pre_total_y
print("Yapo modified: {}".format(yapo_changed))

# ========= CRM PREVIEWS =========
print("\n=== CRM PREVIEWS ===")
crm_owner = []
crm_uncertain = []
crm_excluded = []

for d in col.find({"origen": "toctoc", "listing_id": {"$in": all_new_ids}},
                  {"_id": 0, "listing_id": 1, "url": 1, "comuna": 1, "title": 1,
                   "description": 1, "seller_name": 1, "seller_type": 1,
                   "classification": 1}):
    rec = {
        "listing_id": d.get("listing_id"), "url": d.get("url"),
        "comuna": d.get("comuna"), "title": d.get("title"),
        "seller_name": d.get("seller_name"), "seller_type": d.get("seller_type"),
        "state": d.get("classification", {}).get("state"),
        "confidence": d.get("classification", {}).get("confidence"),
        "reason": d.get("classification", {}).get("reason", ""),
        "deepseek_status": d.get("classification", {}).get("deepseek_status", "N/A"),
    }
    state = d.get("classification", {}).get("state")
    if state == "DUEÑO_SEGURO":
        crm_owner.append(rec)
    elif state == "INCIERTO":
        crm_uncertain.append(rec)
    else:
        crm_excluded.append(rec)

json.dump(crm_owner, open(OUT / "crm_owner_preview.json", "w"), ensure_ascii=False, indent=2)
json.dump(crm_uncertain, open(OUT / "manual_review_uncertain.json", "w"), ensure_ascii=False, indent=2)
print("DUEÑO_SEGURO: {} (preview saved)".format(len(crm_owner)))
print("INCIERTO: {} (review saved)".format(len(crm_uncertain)))
print("Excluded: {}".format(len(crm_excluded)))

# ========= FINAL REPORT =========
report = {
    "run_id": RUN_ID,
    "duration_s": round(total_time, 2),
    "target_met": candidates_processed >= MIN_CANDIDATES,
    "candidates_processed": candidates_processed,
    "searches_executed": idx + 1 if 'idx' in dir() else 0,
    "pre_snapshot": snapshot,
    "post_snapshot": {
        "toctoc_total": post_total_t, "yapo_total": post_total_y,
        "states": post_states, "with_html_path": post_html,
        "with_deepseek_status": post_ds, "yapo_changed": yapo_changed,
    },
    "crm_preview": {"owner": len(crm_owner), "uncertain": len(crm_uncertain), "excluded": len(crm_excluded)},
    "search_stats": search_stats,
}

json.dump(report, open(OUT / "canary_report.json", "w"), ensure_ascii=False, indent=2)
print("\nReport saved to:", OUT)
print("DONE")
