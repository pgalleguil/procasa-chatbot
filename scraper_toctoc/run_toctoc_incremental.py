#!/usr/bin/env python
"""run_toctoc_incremental.py — daily incremental checker for casa/departamento only.
Uses GetProps API. Skips non-target property types. Only processes new listings."""
from __future__ import annotations
import argparse, json, sys, time, os
from pathlib import Path
sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\scraper_toctoc')
os.chdir(r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\scraper_toctoc')

from config import AppConfig
from mongo_store import MongoStore
from discovery import listing_id_from_url, classify_url_format, classify_discovery_candidate, SKIP_PROFESSIONAL, is_target_property_type
from downloader import download_html
from extractor import extract_listing_fields, fetch_gallery_images
from enrich import _enrich_property_fields
from classifier_rules import classify_structural_broker, classify_structural_owner, classify_obvious_broker, build_rule_context
from html_store import save_html, html_path as hs_html_path, sha256_text
import requests as r_lib

def main():
    parser = argparse.ArgumentParser(description="Toctoc Incremental Residential Scraper")
    parser.add_argument("--tipo", default="casa,departamento")
    parser.add_argument("--operacion", default="venta,arriendo")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--proxy-mode", default="proxy")
    parser.add_argument("--write-db", action="store_true")
    args = parser.parse_args()
    
    config = AppConfig()
    mongo = MongoStore(config)
    col = mongo.collection()
    pre_ids = set(str(d['listing_id']) for d in col.find({'origen': 'toctoc'}, {'listing_id': 1, '_id': 0}))
    
    # Read token
    token = ""
    try:
        audit = json.loads(open(r'reports/getprops_audit.json', 'rb').read().decode('latin-1'))
        for req in audit['captured_requests']:
            if 'GetProps' in req.get('url','') and req.get('method') == 'POST':
                token = req.get('headers',{}).get('x-access-token','')
                if token: break
    except: pass
    
    tipos = [t.strip() for t in args.tipo.split(',')]
    operaciones = [o.strip() for o in args.operacion.split(',')]
    OP_MAP = {"venta": 1, "arriendo": 2}
    
    RUN_ID = "incremental_" + __import__('datetime').datetime.now(__import__('datetime').timezone.utc).strftime("%Y%m%d_%H%M%S")
    
    total_new = 0
    total_api = 0
    total_raw = 0
    total_skip_type = 0
    total_prof = 0
    total_hist = 0
    total_ok = 0
    total_ds = 0
    total_err = 0
    total_insert = 0
    
    session = None
    if args.proxy_mode == "proxy":
        try:
            from proxy_manager import load_proxies_from_env
            pool = load_proxies_from_env()
            if pool:
                session = r_lib.Session()
        except: pass
    
    g_session = r_lib.Session()
    g_session.headers.update({
        "Accept":"application/json","Content-Type":"application/json",
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Origin":"https://www.toctoc.com","Accept-Language":"es-CL",
        "sec-ch-ua":'"HeadlessChrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile":"?0","sec-ch-ua-platform":'"Windows"',
        "x-access-token":token})
    g_session.get("https://www.toctoc.com/", timeout=10)
    
    base_payload = {"region":"metropolitana","comuna":"la-florida","barrio":"","poi":"","tipoVista":"lista","idPoligono":None,"moneda":2,"precioDesde":0,"precioHasta":0,"dormitoriosDesde":0,"dormitoriosHasta":0,"banosDesde":0,"banosHasta":0,"estado":2,"publicador":2,"disponibilidadEntrega":"","numeroDeDiasTocToc":0,"superficieDesdeUtil":0,"superficieHastaUtil":0,"superficieDesdeConstruida":0,"superficieHastaConstruida":0,"superficieDesdeTerraza":0,"superficieHastaTerraza":0,"superficieDesdeTerreno":0,"superficieHastaTerreno":0,"ordenarPor":0,"pagina":1,"paginaInterna":1,"zoom":15,"idZonaHomogenea":0,"limite":510,"temporalidad":0,"busqueda":"","santander":False,"primeraCarga":True,"cargaBanner":True,"atributos":[],"viewport":""}
    
    for op in operaciones:
        for tp in tipos:
            print(f"\n--- {op}/{tp} ---")
            payload = dict(base_payload)
            payload["operacion"] = OP_MAP.get(op, 1)
            payload["tipoPropiedad"] = tp
            
            for page in range(1, args.max_pages + 1):
                payload["pagina"] = page
                payload["paginaInterna"] = page
                
                try:
                    resp = g_session.post("https://www.toctoc.com/api/mapa/GetProps", json=payload, timeout=15)
                    raw = resp.json().get("resultados", {}).get("Propiedades") or []
                except:
                    break
                
                total_api += 1
                total_raw += len(raw)
                page_new = 0
                
                for prop in raw:
                    if len(prop) < 45: continue
                    url = str(prop[40]) if prop[40] else ""
                    lid = str(prop[1])
                    if not url or not lid: continue
                    
                    # Property type filter
                    if not is_target_property_type(url):
                        total_skip_type += 1
                        continue
                    
                    # Already in MongoDB
                    if lid in pre_ids:
                        total_hist += 1
                        continue
                    pre_ids.add(lid)  # prevent re-processing within run
                    
                    # Format filter
                    fmt = classify_url_format(url)
                    if classify_discovery_candidate(fmt) == SKIP_PROFESSIONAL:
                        total_prof += 1
                        continue
                    
                    # New candidate — download
                    dl = None
                    for attempt in range(2):
                        try:
                            dl = download_html(url, config, attempt=attempt, session=session)
                            if dl and dl.validation_status in ("INVALID","BLOCKED") and attempt == 0:
                                continue
                            break
                        except: continue
                    
                    if not dl or not dl.html:
                        continue
                    
                    total_ok += 1
                    save_html(url, dl.html, RUN_ID, {"listing_id": lid, "canonical_url": url})
                    hp = str(hs_html_path(url, RUN_ID)) if hs_html_path(url, RUN_ID) else ""
                    hs = sha256_text(dl.html) if dl.html else ""
                    
                    ext = extract_listing_fields(dl.html, source_url=url)
                    if not ext.get("images"):
                        ext["images"] = fetch_gallery_images(dl.html, url)
                    enr = _enrich_property_fields(ext, url, config.uf_valor_clp, config.uf_fecha)
                    
                    if classify_structural_broker(ext):
                        cls = classify_structural_broker(ext)
                    elif classify_structural_owner(ext):
                        cls = classify_structural_owner(ext)
                    else:
                        rr = classify_obvious_broker(ext)
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
                                cls = {"state": "INCIERTO", "confidence": 0.3, "reason": f"DS skipped: {ds_reason}", "source": "rules_fallback", "deepseek_status": "NOT_NEEDED"}
                            else:
                                from deepseek_classifier import classify_with_deepseek, DeepSeekStatus as DS
                                try:
                                    ds = classify_with_deepseek(ext, rctx, config)
                                    total_ds += 1
                                    if ds and ds.status == DS.VALID.value:
                                        cls = {"state": ds.state, "confidence": ds.confidence, "reason": ds.reason, "evidence": ds.evidence, "source": "deepseek", "deepseek_raw": ds.raw, "deepseek_status": ds.status}
                                    elif ds:
                                        total_err += 1
                                        cls = {"state": "INCIERTO", "confidence": 0.3, "reason": f"DS {ds.status}: {ds.reason}. Fallback.", "source": "rules_fallback", "deepseek_status": ds.status}
                                    else:
                                        cls = {"state": "INCIERTO", "confidence": 0.3, "reason": "DS no result", "source": "rules_fallback", "deepseek_status": DS.NOT_NEEDED.value}
                                except:
                                    total_err += 1
                                    cls = {"state": "INCIERTO", "confidence": 0.3, "reason": "DS error", "source": "error", "deepseek_status": DS.API_ERROR.value}
                    
                    dl_len = len(enr.get("description", enr.get("descripcion", "")))
                    rec = {**enr, "classification": cls, "processed_at": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(), "batch_id": RUN_ID, "source": "owner_hunt", "origen": "toctoc", "source_portal": "toctoc", "schema_version": "crm_v1", "html_path": hp, "html_sha256": hs, "description_length": dl_len}
                    try:
                        mongo.ensure_index()
                        r = mongo.upsert_listing(rec)
                        if r.get("upserted_id"): total_insert += 1
                        page_new += 1
                        total_new += 1
                    except: pass
                
                print(f"  Page {page}: {len(raw):4d} raw, {page_new:3d} new")
                if page_new == 0:
                    break
    
    print(f"\n{'='*60}")
    print(f"INCREMENTAL RUN COMPLETE")
    print(f"{'='*60}")
    print(f"  API calls: {total_api}")
    print(f"  Raw props: {total_raw}")
    print(f"  Skip (non-target type): {total_skip_type}")
    print(f"  Historical (in MongoDB): {total_hist}")
    print(f"  Professional filtered: {total_prof}")
    print(f"  Downloaded OK: {total_ok}")
    print(f"  New candidates: {total_new}")
    print(f"  DeepSeek calls: {total_ds}")
    print(f"  DeepSeek errors: {total_err}")
    print(f"  MongoDB inserts: {total_insert}")
    
    post_t = col.count_documents({"origen": "toctoc"})
    post_y = col.count_documents({"origen": "yapo"})
    print(f"  Toctoc: {post_t}")
    print(f"  Yapo: {post_y} (changed={post_y != 5116})")

    if args.write_db and total_insert > 0:
        _run_post_scrape_distribution()


def _run_post_scrape_distribution():
    """Dispara la distribucion de captaciones nuevas tras persistir el lote."""
    import subprocess
    root = Path(r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok').resolve()
    script = root / "scripts" / "run_distribution_after_scrape.py"
    if not script.exists():
        print(f"  [POST-SCRAPE] Script de distribucion no encontrado: {script}")
        return
    try:
        r = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=300,
        )
        if r.stdout.strip():
            print(r.stdout.strip())
        if r.returncode != 0 and r.stderr.strip():
            print(f"  [POST-SCRAPE] stderr: {r.stderr.strip()[-400:]}")
    except Exception as e:
        print(f"  [POST-SCRAPE] No se pudo distribuir: {e}")

if __name__ == "__main__":
    main()
