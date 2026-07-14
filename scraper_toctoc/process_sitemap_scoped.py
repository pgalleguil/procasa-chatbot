#!/usr/bin/env python
"""process_sitemap_scoped.py — Process sitemap candidates with configurable filters.
Filters: region, comuna, property type, operation, price.
Usage:
  python process_sitemap_scoped.py --region metropolitana,maule --tipo casa,departamento --batch-size 200 --proxy-mode proxy --write-db
  python process_sitemap_scoped.py --resume --batch-size 200 --proxy-mode proxy --write-db
"""
import argparse, json, sys, os, time, re, hashlib
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\scraper_toctoc')
sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
os.chdir(r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\scraper_toctoc')
import requests as r_lib
from config import AppConfig
from mongo_store import MongoStore
from downloader import download_html
from extractor import extract_listing_fields, fetch_gallery_images
from enrich import _enrich_property_fields
from classifier_rules import classify_structural_broker, classify_structural_owner, classify_obvious_broker, build_rule_context
from deepseek_classifier import DeepSeekStatus as DS
from html_store import save_html, html_path as hs_html_path, sha256_text


def compute_publisher_activity(current, history, window_days=90):
    """Explainable temporal activity; never classifies by count alone."""
    ids = {str(d.get("listing_id") or d.get("url") or "") for d in history}
    ids.discard("")
    return {
        "window_days": window_days,
        "linked_publications": len(history),
        "distinct_properties": len(ids),
        "confirmed_broker_count": sum(
            1 for d in history if str((d.get("classification") or {}).get("state")) == "CORREDOR_SEGURO"
        ),
        "rule": "count_alone_never_classifies",
    }

config = AppConfig()
_mongo = None
_col = None
def _get_col():
    global _mongo, _col
    if _col is None:
        _mongo = MongoStore(config)
        _col = _mongo.collection()
    return _col
OUT = Path(r'reports/sitemap_scoped_inventory')
OUT.mkdir(exist_ok=True)

# Default scope
SCOPE = {
    "metropolitana": ["*"],
    "maule": ["talca"],
}
ALLOWED_TYPES = {"casa", "departamento"}
ALLOWED_OPERATIONS = {"venta", "arriendo"}
BLOCKED_TYPES = {"estacionamiento"}

# Normalization maps
REGION_MAP = {
    'metropolitana': 'metropolitana', 'region metropolitana': 'metropolitana',
    'maule': 'maule', 'region del maule': 'maule', 'region maule': 'maule',
}
_COMUNA_ACCENTS = str.maketrans('áéíóúñüÁÉÍÓÚÑÜ', 'aeiounuAEIOUNU')

def _norm_region(r):
    r = r.strip().lower().replace('-', ' ').replace('_', ' ')
    r = re.sub(r'\s+', ' ', r).strip()
    return REGION_MAP.get(r, r.translate(_COMUNA_ACCENTS).replace(' ', '-'))

def _norm_comuna(c):
    c = c.strip().lower().replace('-', ' ').replace('_', ' ')
    c = c.translate(_COMUNA_ACCENTS)
    c = re.sub(r'\s+', ' ', c).strip().replace(' ', '-')
    return c

def _norm_tipo(t):
    t = t.strip().lower().replace('-', ' ').replace('_', ' ')
    # Singular/plural normalization
    mapping = {
        'casa': 'casa', 'casas': 'casa',
        'departamento': 'departamento', 'departamentos': 'departamento',
        'bodega': 'bodega', 'bodegas': 'bodega',
        'local': 'local-comercial', 'locales': 'local-comercial', 'local-comercial': 'local-comercial', 'local comercial': 'local-comercial',
        'oficina': 'oficina', 'oficinas': 'oficina',
        'terreno': 'terreno', 'terrenos': 'terreno',
        'parcela': 'parcela', 'parcelas': 'parcela',
        'sitio': 'sitio', 'sitios': 'sitio',
        'estacionamiento': 'estacionamiento', 'estacionamientos': 'estacionamiento',
        'industrial': 'industrial',
    }
    return mapping.get(t, t)

def _is_in_scope(region, comuna):
    r = _norm_region(region or '')
    c = _norm_comuna(comuna or '')
    scope = SCOPE.get(r, [])
    if not scope:
        return False
    if '*' in scope:
        return True
    return c in scope

def _check_price(min_uf, max_uf, min_clp, max_clp, precio_uf, precio_clp):
    if min_uf is None and max_uf is None and min_clp is None and max_clp is None:
        return True  # no filter
    if min_uf is not None and precio_uf is not None and precio_uf < min_uf:
        return False
    if max_uf is not None and precio_uf is not None and precio_uf > max_uf:
        return False
    if min_clp is not None and precio_clp is not None and precio_clp < min_clp:
        return False
    if max_clp is not None and precio_clp is not None and precio_clp > max_clp:
        return False
    return True

def _scope_hash(scope, tipos, ops):
    raw = json.dumps({"scope": scope, "tipos": sorted(tipos), "ops": sorted(ops)}, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:16]

def main():
    global SCOPE
    parser = argparse.ArgumentParser(description="Sitemap scoped processor")
    parser.add_argument("--region", default="metropolitana,maule")
    parser.add_argument("--comuna", default="")
    parser.add_argument("--all-communes", action="store_true")
    parser.add_argument("--tipo", default="casa,departamento")
    parser.add_argument("--operacion", default="venta,arriendo")
    parser.add_argument("--precio-desde-uf", type=float, default=None)
    parser.add_argument("--precio-hasta-uf", type=float, default=None)
    parser.add_argument("--precio-desde-clp", type=int, default=None)
    parser.add_argument("--precio-hasta-clp", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--proxy-mode", default="proxy")
    parser.add_argument("--write-db", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--test-dry-run", action="store_true", help="Alias for --dry-run")
    args = parser.parse_args()
    
    RUN_ID = "sitemap_scoped_" + __import__('datetime').datetime.now(__import__('datetime').timezone.utc).strftime("%Y%m%d_%H%M%S")
    
    # Build scope from args
    scope = {}
    for region_str in args.region.split(','):
        r = _norm_region(region_str.strip())
        scope[r] = ["*"]
    
    # If specific communes are supplied, replace wildcard region coverage with
    # exactly those communes. This keeps targeted canaries truly targeted.
    if args.comuna:
        scope = {region: [] for region in scope}
        for pair in args.comuna.split(','):
            if ':' in pair:
                r, c = pair.split(':', 1)
                rn = _norm_region(r.strip())
                cn = _norm_comuna(c.strip())
            else:
                cn = _norm_comuna(pair.strip())
                rn = list(scope.keys())[0] if scope else 'metropolitana'
            if rn not in scope:
                scope[rn] = []
            if '*' not in scope.get(rn, []):
                scope.setdefault(rn, [])
                scope[rn].append(cn)
    SCOPE = scope
    
    # Normalize tipos
    tipos = [_norm_tipo(t) for t in args.tipo.split(',')]
    ops = [o.strip().lower() for o in args.operacion.split(',')]
    
    # Price filters
    price_filters = {
        "min_uf": args.precio_desde_uf,
        "max_uf": args.precio_hasta_uf,
        "min_clp": args.precio_desde_clp,
        "max_clp": args.precio_hasta_clp,
    }
    
    current_hash = _scope_hash(scope, tipos, ops)
    
    # Load queue
    queue_path = OUT / 'queue.jsonl'
    if not queue_path.exists():
        print("Queue file not found. Run build_scoped_inventory.py first.")
        return
    
    with open(queue_path, 'r') as f:
        queue = [json.loads(line) for line in f]
    
    # Check scope hash if resuming
    queue_hash_path = OUT / 'scope_hash.txt'
    if args.resume and queue_hash_path.exists():
        stored_hash = queue_hash_path.read_text().strip()
        if stored_hash != current_hash:
            print(f"ERROR: Scope hash mismatch. Stored: {stored_hash}, Current: {current_hash}")
            print("Cannot resume with different scope. Run without --resume to rebuild.")
            return
    
    # Save scope hash
    queue_hash_path.write_text(current_hash)
    
    # Recover state from MongoDB
    pre_ids = set(str(d['listing_id']) for d in _get_col().find({'origen': 'toctoc'}, {'listing_id': 1, '_id': 0}))
    
    # Process queue statuses
    for c in queue:
        lid = c['listing_id']
        s = c.get('new_status', '')
        
        # Recover IN_PROGRESS that were actually processed
        if s in ('IN_PROGRESS', 'PENDING_NEW_IN_SCOPE') and lid in pre_ids:
            c['new_status'] = 'PROCESSED'
        elif s == 'IN_PROGRESS' and lid not in pre_ids:
            c['new_status'] = 'PENDING_NEW_IN_SCOPE'
        
        # Re-evaluate scope
        if c['new_status'] in ('PENDING_NEW_IN_SCOPE', 'SKIP_OUT_OF_COVERAGE', 'FAILED_RETRYABLE'):
            c['new_status'] = 'PENDING_NEW_IN_SCOPE'
            region = c.get('region', '')
            comuna = c.get('comuna', '')
            tipo = _norm_tipo(c.get('tipo_propiedad', ''))
            op = c.get('operacion', '')
            
            if not _is_in_scope(region, comuna):
                c['new_status'] = 'SKIP_OUT_OF_COVERAGE'
            elif tipo not in tipos:
                c['new_status'] = 'SKIP_NON_SELECTED_PROPERTY_TYPE'
            elif tipo in BLOCKED_TYPES:
                c['new_status'] = 'SKIP_NON_SELECTED_PROPERTY_TYPE'
            elif op and op not in ops:
                c['new_status'] = 'SKIP_OUT_OF_COVERAGE'
    
    # Stats
    statuses = defaultdict(int)
    for c in queue:
        statuses[c['new_status']] += 1
    
    print(f"\n=== QUEUE STATUS ===")
    for s, cnt in sorted(statuses.items(), key=lambda x: -x[1]):
        print(f"  {s}: {cnt}")
    
    pending = [c for c in queue if c['new_status'] == 'PENDING_NEW_IN_SCOPE']
    print(f"\nPENDING_NEW_IN_SCOPE: {len(pending)}")
    
    if args.dry_run or args.test_dry_run:
        print(f"\n=== DRY RUN ===")
        print(f"Scope regions: {list(scope.keys())}")
        print(f"Tipos: {tipos}")
        print(f"Ops: {ops}")
        print(f"Price filters: {price_filters}")
        print(f"Pending would be processed: {len(pending)}")
        print("Processing would start with:")
        for p in pending[:5]:
            print(f"  {p['listing_id']}: {p.get('region','')}/{p.get('comuna','')} {p.get('operacion','')}/{p.get('tipo_propiedad','')}")
        print("DRY RUN COMPLETE. No changes made.")
        return

    # Save updated queue only for an actual processing run.
    with open(queue_path, 'w', encoding='utf-8') as f:
        for c in queue:
            f.write(json.dumps(c, ensure_ascii=False) + '\n')
    
    if not pending:
        print("No pending candidates. Nothing to process.")
        return
    
    # Proxy
    proxy_session = None
    try:
        from proxy_manager import load_proxies_from_env
        pool = load_proxies_from_env()
        if pool: proxy_session = r_lib.Session()
    except: pass
    
    # Process batches
    BATCH_SIZE = args.batch_size
    total_processed = 0
    total_inserts = 0
    batch_num = len(list(OUT.glob('batch_*'))) + 1
    all_new_owners = []
    
    while pending:
        batch = pending[:BATCH_SIZE]
        pending = pending[BATCH_SIZE:]
        batch_dir = OUT / f"batch_{batch_num:03d}"
        batch_dir.mkdir(exist_ok=True)
        batch_start = time.time()
        
        ok = 0; fail = 0; html = 0; ds_calls = 0; ds_err = 0; fb = 0; inserts = 0
        skip_price = 0; batch_owners = []
        
        # Re-check MongoDB
        current_ids = set(str(d['listing_id']) for d in _get_col().find({'origen': 'toctoc'}, {'listing_id': 1, '_id': 0}))
        batch = [b for b in batch if b['listing_id'] not in current_ids]
        
        print(f"\nBatch {batch_num}: {len(batch)} candidates")
        
        for item in batch:
            lid = item['listing_id']; url = item['url']
            item['new_status'] = 'IN_PROGRESS'
            
            dl = None
            # Reuse a previously validated local backup before touching the portal.
            # This also lets interrupted Mongo writes be recovered without re-scraping.
            for meta_path in sorted((Path(__file__).resolve().parent / "html_dumps").glob("**/*.json"), reverse=True):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    cached_path = meta_path.with_suffix(".html")
                    if str(meta.get("listing_id", "")) == lid and cached_path.exists():
                        from types import SimpleNamespace
                        dl = SimpleNamespace(
                            html=cached_path.read_text(encoding="utf-8"),
                            validation_status="CACHED_VALID",
                        )
                        break
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
            for attempt in range(2):
                if dl is not None:
                    break
                try:
                    dl = download_html(url, config, attempt=attempt, session=proxy_session)
                    if dl and dl.validation_status in ("INVALID","BLOCKED") and attempt == 0: continue
                    break
                except: continue
            if not dl or not dl.html:
                fail += 1
                item['new_status'] = 'FAILED_RETRYABLE'
                continue
            ok += 1
            
            save_html(url, dl.html, RUN_ID, {"listing_id": lid, "canonical_url": url})
            html += 1
            hp = str(hs_html_path(url, RUN_ID)) if hs_html_path(url, RUN_ID) else ""
            hs = sha256_text(dl.html) if dl.html else ""
            
            ext = extract_listing_fields(dl.html, source_url=url)
            if not ext.get("images"):
                ext["images"] = fetch_gallery_images(dl.html, url, session=proxy_session)
            enr = _enrich_property_fields(ext, url, config.uf_valor_clp, config.uf_fecha)
            identity_values = [
                ext.get("seller_profile_id"), ext.get("publicador_visible"),
                ext.get("company_name"), ext.get("broker_brand"),
            ]
            identity_values = [str(v).strip() for v in identity_values if str(v or "").strip()]
            history = []
            if identity_values:
                history = list(_get_col().find({
                    "$or": [
                        {"seller_profile_id": {"$in": identity_values}},
                        {"publicador_visible": {"$in": identity_values}},
                        {"company_name": {"$in": identity_values}},
                        {"broker_brand": {"$in": identity_values}},
                    ]
                }, {
                    "_id": 0, "seller_profile_id": 1, "publicador_visible": 1,
                    "company_name": 1, "broker_brand": 1, "listing_id": 1,
                    "url": 1, "processed_at": 1, "fecha_publicacion": 1,
                }).limit(200))
            ext["processed_at"] = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
            ext["listing_id"] = lid
            ext["url"] = url
            ext["publisher_activity"] = compute_publisher_activity(ext, history, window_days=90)
            enr["publisher_activity"] = ext["publisher_activity"]
            
            # PRICE FILTER
            precio_uf = enr.get('precio_uf') or enr.get('price_uf')
            precio_clp = enr.get('precio_clp') or enr.get('price_clp')
            if not _check_price(
                price_filters['min_uf'], price_filters['max_uf'],
                price_filters['min_clp'], price_filters['max_clp'],
                precio_uf, precio_clp
            ):
                skip_price += 1
                item['new_status'] = 'SKIP_PRICE_UNKNOWN'
                continue
            
            # Classify
            rule_result = classify_structural_broker(ext) or classify_structural_owner(ext) or classify_obvious_broker(ext)
            rule_state = (rule_result or {}).get("state", "INCONCLUSIVE")
            if rule_result:
                cls = rule_result
            else:
                    rr = None
                    if rr: cls = rr
                    else:
                        rctx = build_rule_context(ext)
                        from classifier_rules import should_invoke_deepseek
                        must_invoke, ds_reason = should_invoke_deepseek(
                            rule_state="INCONCLUSIVE",
                            description=ext.get("description", ext.get("descripcion", "")),
                            description_length=len(ext.get("description", ext.get("descripcion", ""))),
                            seller_type=str(ext.get("seller_type", "")),
                        )
                        if not must_invoke:
                            fb += 1
                            cls = {"state": "INCIERTO", "confidence": 0.3, "reason": f"DS skipped: {ds_reason}", "source": "rules_fallback", "deepseek_status": DS.NOT_NEEDED.value}
                        else:
                            from deepseek_classifier import classify_with_deepseek, DeepSeekStatus as DS
                            try:
                                ds = classify_with_deepseek(ext, rctx, config)
                                ds_calls += 1
                                if ds and ds.status == DS.VALID.value:
                                    cls = {"state": ds.state, "confidence": ds.confidence, "reason": ds.reason, "evidence": ds.evidence, "source": "deepseek", "deepseek_raw": ds.raw, "deepseek_status": ds.status,
                                           "deepseek_payload": ds.payload, "deepseek_message_content": ds.message_content,
                                           "deepseek_reasoning_content": ds.reasoning_content}
                                elif ds:
                                    ds_err += 1; fb += 1
                                    cls = {"state": "INCIERTO", "confidence": 0.3, "reason": f"DS {ds.status}: {ds.reason}. Fallback.", "source": "rules_fallback", "deepseek_status": ds.status}
                                else:
                                    fb += 1; cls = {"state": "INCIERTO", "confidence": 0.3, "reason": "DS no result", "source": "rules_fallback", "deepseek_status": DS.NOT_NEEDED.value}
                            except:
                                ds_err += 1; fb += 1; cls = {"state": "INCIERTO", "confidence": 0.3, "reason": "DS error", "source": "error", "deepseek_status": DS.API_ERROR.value}
            
            if cls.get('state') == 'DUEÑO_SEGURO':
                batch_owners.append({"listing_id": lid, "url": url, "comuna": item.get('comuna',''), "tipo": item.get('tipo_propiedad',''), "reason": cls.get('reason','')[:100], "evidence": cls.get('evidence',[]), "state_source": cls.get('source','rules')})
                all_new_owners.append(batch_owners[-1])
            
            cls["rule_state"] = rule_state
            cls["final_state"] = cls.get("state")
            cls["rules_version"] = "toctoc-owner-rules-v2"
            cls["prompt_version"] = "toctoc-deepseek-owner-v2"
            cls["analysis_at"] = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
            cls["trace"] = {
                "rule_state": rule_state, "final_state": cls.get("state"),
                "deepseek_payload": cls.get("deepseek_payload", {}),
                "deepseek_message_content": cls.get("deepseek_message_content", ""),
                "deepseek_reasoning_content": cls.get("deepseek_reasoning_content", ""),
                "deepseek_raw": cls.get("deepseek_raw", {}),
                "rules_version": cls["rules_version"], "prompt_version": cls["prompt_version"],
                "analysis_at": cls["analysis_at"],
            }
            dl_len = len(enr.get("description", enr.get("descripcion", "")))
            rec = {
                **enr,
                "listing_id": lid,
                "url": url,
                "canonical_url": url,
                "comuna": enr.get("comuna") or item.get("comuna", ""),
                "region": enr.get("region") or item.get("region", ""),
                "operacion": enr.get("operacion") or item.get("operacion", ""),
                "tipo_propiedad": enr.get("tipo_propiedad") or item.get("tipo_propiedad", ""),
                "company_name": ext.get("company_name", ""),
                "broker_brand": ext.get("broker_brand", ""),
                "seller_type": ext.get("seller_type", ""),
                "seller_is_pro": bool(ext.get("seller_is_pro")),
                "publicador_visible": ext.get("publicador_visible") or ext.get("contact_name") or "",
                "seller_profile_id": ext.get("seller_profile_id", ""),
                "classification": cls,
                "classifier_original_signals": {
                    "state": cls.get("state"), "source": cls.get("source"),
                    "reason": cls.get("reason"), "evidence": cls.get("evidence", []),
                    "signals": cls.get("signals", {}),
                },
                "processed_at": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
                "batch_id": RUN_ID, "source": "owner_hunt", "origen": "toctoc",
                "source_portal": "toctoc", "schema_version": "crm_v1",
                "html_path": hp, "html_sha256": hs, "description_length": dl_len,
            }
            try:
                _mongo.ensure_index()
                r = _mongo.upsert_listing(rec)
                if r.get("upserted_id"): inserts += 1
            except Exception as exc:
                item['new_status'] = 'FAILED_RETRYABLE'
                print(f"  Mongo upsert failed for {lid}: {exc}")
                continue
            
            item['new_status'] = 'PROCESSED'
        
        duration = round(time.time() - batch_start, 1)
        total_processed += len(batch)
        total_inserts += inserts
        
        rpt = {"batch_number": batch_num, "run_id": RUN_ID, "duration_s": duration, "candidates_in_batch": len(batch), "downloads_ok": ok, "downloads_fail": fail, "html_saved": html, "deepseek_calls": ds_calls, "deepseek_errors": ds_err, "fallbacks": fb, "mongo_inserts": inserts, "skip_price": skip_price, "new_owners": len(batch_owners), "pending_remaining": len(pending)}
        with open(batch_dir / 'report.json', 'w') as f: json.dump(rpt, f, ensure_ascii=False, indent=2)
        with open(batch_dir / 'new_owners.json', 'w') as f: json.dump(batch_owners, f, ensure_ascii=False, indent=2)
        
        print(f"  Duration: {duration}s, OK: {ok}, Fail: {fail}, Inserts: {inserts}, Remaining: {len(pending)}")
        
        # Save queue after each batch
        with open(queue_path, 'w', encoding='utf-8') as f:
            for c in queue:
                f.write(json.dumps(c, ensure_ascii=False) + '\n')
        
        if fail > 0 and fail / max(len(batch), 1) > 0.3:
            print(f"  FAILURE RATE > 30% ({fail}/{len(batch)}). Stopping.")
            break
        
        batch_num += 1
    
    # Final
    post_t = _get_col().count_documents({"origen": "toctoc"})
    post_y = _get_col().count_documents({"origen": "yapo"})
    states = {}
    for d in _get_col().find({'origen': 'toctoc'}, {'classification.state': 1, '_id': 0}):
        s = d.get('classification', {}).get('state', '?')
        states[s] = states.get(s, 0) + 1
    
    print(f"\n{'='*60}")
    print(f"PROCESSING COMPLETE")
    print(f"{'='*60}")
    print(f"  Batches this run: {batch_num - len(list(OUT.glob('batch_*'))) + 1}")
    print(f"  Processed: {total_processed}")
    print(f"  Inserts: {total_inserts}")
    print(f"  Skip price: {skip_price}")
    print(f"  Remaining: {len(pending)}")
    print(f"  New owners: {len(all_new_owners)}")
    print(f"  Toctoc: {post_t}")
    print(f"  Yapo: {post_y} (changed={post_y != 5116})")
    print(f"  States: {json.dumps({str(k):v for k,v in states.items()}, ensure_ascii=False)}")
    
    with open(OUT / 'new_owners_audit.md', 'a', encoding='utf-8') as f:
        for o in all_new_owners:
            f.write(f"- {o['listing_id']}: {o.get('comuna','')} {o.get('tipo','')} | {o.get('reason','')[:100]}\n")
    
    final = {"total_processed": total_processed, "total_inserts": total_inserts, "remaining": len(pending), "new_owners": len(all_new_owners), "toctoc_after": post_t, "yapo": post_y, "yapo_changed": post_y != 5116, "states": {str(k):v for k,v in states.items()}}
    with open(OUT / 'final_report.json', 'w') as f: json.dump(final, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
