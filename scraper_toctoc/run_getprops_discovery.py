"""GetProps Production Run: full pipeline with discovery via /api/mapa/GetProps.
Reuses downloader, extractor, classifier from run_toctoc.py. No SPA, no SSR."""
from __future__ import annotations
import argparse, json, sys, time, os, hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\scraper_toctoc')
from config import AppConfig
from mongo_store import MongoStore
from discovery import listing_id_from_url, classify_url_format
from classifier_rules import (
    classify_structural_broker, classify_structural_owner, classify_obvious_broker,
    build_rule_context, company_shape_in_publisher_fields
)
from extractor import extract_listing_fields, fetch_gallery_images
from enrich import _enrich_property_fields
from downloader import download_html, DownloadResult
from html_store import save_html, load_html, is_valid_local_html, html_path as hs_html_path, sha256_text
from proxy_manager import ProxyManager
import requests as req_lib

# Field positions in GetProps Propiedades array (0-indexed)
IDX_LISTING_ID = 1
IDX_COMUNA = 7
IDX_DORM = 8
IDX_BANOS = 9
IDX_PRECIO_UF = 22
IDX_PRECIO_CLP = 24
IDX_TITLE = 39
IDX_URL = 40

OPERACION_MAP = {"venta": 1, "arriendo": 2}
TIPO_MAP = {"departamento": 8, "casa": 7}


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


_GETPROPS_TOKEN_CACHE: str | None = None

def _getprops_token() -> str:
    """Obtain x-access-token. Reads from cached audit file first, or uses Playwright."""
    global _GETPROPS_TOKEN_CACHE
    if _GETPROPS_TOKEN_CACHE:
        return _GETPROPS_TOKEN_CACHE
    
    # Try reading from the captured audit file
    audit_path = Path(__file__).resolve().parent / "reports" / "getprops_audit.json"
    if audit_path.exists():
        try:
            audit = json.loads(audit_path.read_bytes().decode("latin-1"))
            for req in audit.get("captured_requests", []):
                if "GetProps" in req.get("url", "") and req.get("method") == "POST":
                    token = req.get("headers", {}).get("x-access-token", "")
                    if token:
                        _GETPROPS_TOKEN_CACHE = token
                        return token
        except Exception:
            pass
    
    # Fallback: use Playwright to navigate and capture token
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                viewport={"width": 1920, "height": 1080},
            )
            page = context.new_page()
            token_value = None
            def _capture(route):
                nonlocal token_value
                if "GetProps" in route.request.url:
                    h = dict(route.request.headers)
                    if h.get("x-access-token"):
                        token_value = h["x-access-token"]
                    route.continue_()
                elif route.request.resource_type in ("image", "media", "font"):
                    route.abort()
                else:
                    route.continue_()
            page.route("**/*", _capture)
            page.goto("https://www.toctoc.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            browser.close()
            if token_value:
                _GETPROPS_TOKEN_CACHE = token_value
                return token_value
    except Exception:
        pass
    
    raise RuntimeError("Cannot obtain x-access-token for GetProps API")


# Add TARGET_PROPERTY_TYPES reference to existing discovery
TARGET_PROPERTY_TYPES = frozenset({"casa", "departamento"})

def _getprops_session(operacion_val: int, tipo_val: int, x_token: str = "") -> tuple:
    """Create and return a requests Session pre-configured for GetProps API calls.
    Returns (session, referer_url, payload_template)."""
    if not x_token:
        x_token = _getprops_token()
    
    session = req_lib.Session()
    session.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Origin": "https://www.toctoc.com",
        "Accept-Language": "es-CL",
        "sec-ch-ua": '"HeadlessChrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "x-access-token": x_token,
    })
    
    # Visit homepage first to establish session cookies
    try:
        session.get("https://www.toctoc.com/", timeout=10)
    except Exception:
        pass
    
    referer_url = f"https://www.toctoc.com/resultados/lista/compra/departamento/metropolitana/la-florida/?moneda=2&estado=2"
    
    payload_template = {
        "region": "metropolitana", "comuna": "la-florida",
        "barrio": "", "poi": "", "tipoVista": "lista",
        "operacion": operacion_val,
        "idPoligono": None, "moneda": 2,
        "precioDesde": 0, "precioHasta": 0,
        "dormitoriosDesde": 0, "dormitoriosHasta": 0,
        "banosDesde": 0, "banosHasta": 0,
        "tipoPropiedad": "departamento",
        "estado": 2,
        "publicador": 2,
        "disponibilidadEntrega": "", "numeroDeDiasTocToc": 0,
        "superficieDesdeUtil": 0, "superficieHastaUtil": 0,
        "superficieDesdeConstruida": 0, "superficieHastaConstruida": 0,
        "superficieDesdeTerraza": 0, "superficieHastaTerraza": 0,
        "superficieDesdeTerreno": 0, "superficieHastaTerreno": 0,
        "ordenarPor": 0,
        "pagina": 1, "paginaInterna": 1,
        "zoom": 15, "idZonaHomogenea": 0,
        "limite": 510, "temporalidad": 0,
        "busqueda": "", "santander": False,
        "primeraCarga": True, "cargaBanner": True,
        "atributos": [], "viewport": "",
    }
    
    return session, referer_url, payload_template


def _getprops_page(session, referer_url: str, payload_template: dict, pagina: int) -> list[dict]:
    """Fetch one page from GetProps API using pre-configured session."""
    payload = dict(payload_template)
    payload["pagina"] = pagina
    payload["paginaInterna"] = pagina
    
    session.headers.update({"Referer": referer_url})
    
    try:
        resp = session.post("https://www.toctoc.com/api/mapa/GetProps",
                            json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  GetProps error: {e}")
        return []
    
    resultados = data.get("resultados", {})
    raw_props = resultados.get("Propiedades") or []
    
    props = []
    for raw in raw_props:
        if not isinstance(raw, (list, tuple)) or len(raw) < 45:
            continue
        url = str(raw[IDX_URL]) if raw[IDX_URL] else ""
        if not url:
            continue
        listing_id = str(raw[IDX_LISTING_ID])
        title = str(raw[IDX_TITLE] or "")
        comuna = str(raw[IDX_COMUNA] or "")
        precio_uf = float(raw[IDX_PRECIO_UF] or 0)
        precio_clp = float(raw[IDX_PRECIO_CLP] or 0)
        dorm = int(raw[IDX_DORM] or 0)
        banos = int(raw[IDX_BANOS] or 0)
        url_format = classify_url_format(url)
        
        props.append({
            "url": url,
            "listing_id": listing_id,
            "listing_id_source": "getprops_api",
            "url_format": url_format,
            "title": title,
            "comuna": comuna,
            "precio_uf": precio_uf,
            "precio_clp": precio_clp,
            "dormitorios": dorm,
            "banos": banos,
            "operacion": "",
            "tipo_propiedad": "",
        })
    
    return props


def main():
    parser = argparse.ArgumentParser(description="GetProps Production Run")
    parser.add_argument("--operacion", default="venta,arriendo",
                        help="Comma-separated: venta,arriendo")
    parser.add_argument("--tipo", default="departamento,casa",
                        help="Comma-separated: departamento,casa")
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--target-new-candidates", type=int, default=150)
    parser.add_argument("--max-new-candidates", type=int, default=250)
    parser.add_argument("--proxy-mode", default="proxy")
    parser.add_argument("--write-db", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    
    config = AppConfig()
    RUN_ID = "getprops_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    OUT = config.reports_dir / "getprops_production_run" / RUN_ID
    OUT.mkdir(parents=True, exist_ok=True)
    
    # Snapshot
    mongo = MongoStore(config) if args.write_db else None
    if mongo:
        col = mongo.collection()
        pre_t = col.count_documents({"origen": "toctoc"})
        pre_y = col.count_documents({"origen": "yapo"})
        pre_states = {}
        for s in col.aggregate([{"$match": {"origen": "toctoc"}},
                                {"$group": {"_id": "$classification.state", "count": {"$sum": 1}}}]):
            pre_states[s["_id"]] = s["count"]
        pre_tids = set(str(d["listing_id"]) for d in col.find({"origen": "toctoc"}, {"listing_id": 1, "_id": 0}))
        pre_yids = set(str(d["listing_id"]) for d in col.find({"origen": "yapo"}, {"listing_id": 1, "_id": 0}))
    else:
        pre_t = pre_y = 0
        pre_states = {}
        pre_tids = set()
        pre_yids = set()
    
    print(f"=== GETPROPS PRODUCTION RUN ===")
    print(f"Target: {args.target_new_candidates}-{args.max_new_candidates} new")
    print(f"Max pages per combo: {args.max_pages}")
    print(f"Toctoc pre: {pre_t} | Yapo pre: {pre_y}")
    
    # Proxy
    proxy_manager = ProxyManager.from_env() if args.proxy_mode == "proxy" else None
    proxy_pool = []
    if proxy_manager and proxy_manager.has_proxies():
        from proxy_manager import load_proxies_from_env
        proxy_pool = load_proxies_from_env()
        print(f"Proxy pool: {len(proxy_pool)} proxies")
    
    operaciones = [o.strip() for o in args.operacion.split(",")]
    tipos = [t.strip() for t in args.tipo.split(",")]
    
    global_seen_ids = set(pre_tids)
    global_seen_urls: set[str] = set()
    all_new_ids = []
    candidates_processed = 0
    total_api_calls = 0
    total_raw_props = 0
    prof_skipped = 0
    hist_duplicates = 0
    run_duplicates = 0
    download_ok = 0
    download_fail = 0
    deepseek_calls = 0
    deepseek_errors = 0
    fallbacks_used = 0
    html_saved = 0
    mongo_inserts = 0
    mongo_updates = 0
    total_proxy_bytes = 0
    per_combo_metrics = []
    
    total_start = time.time()
    session = None
    block = {"count": 0, "max": 25, "idx": 0}
    
    def _open_block():
        nonlocal session
        if not proxy_pool:
            return None, None
        if session:
            try:
                session.close()
            except:
                pass
        block["idx"] = (block["idx"] + 1) % len(proxy_pool)
        p_url = proxy_pool[block["idx"]]
        block["count"] = 0
        import requests as r2
        session = r2.Session()
        return p_url, session
    
    p_url, dl_session = _open_block() if proxy_pool else (None, None)
    
    # Initialize GetProps API session
    gp_session, gp_referer, gp_template = _getprops_session(OPERACION_MAP.get("venta", 1), TIPO_MAP.get("departamento", 8))
    
    for op in operaciones:
        for tp in tipos:
            if candidates_processed >= args.max_new_candidates:
                break
            
            combo_label = f"{op}_{tp}"
            print(f"\n{'='*60}")
            print(f"[{combo_label}]")
            print(f"{'='*60}")
            
            op_val = OPERACION_MAP.get(op, 1)
            tp_val = TIPO_MAP.get(tp, 8)
            combo_new = 0
            
            # Update GetProps session for this combo
            gp_session.headers.update({"Referer": gp_referer.replace("compra", "compra" if op == "venta" else "arriendo")})
            gp_template_this = dict(gp_template)
            gp_template_this["operacion"] = op_val
            gp_template_this["tipoPropiedad"] = tp
            
            for page in range(1, args.max_pages + 1):
                if candidates_processed >= args.max_new_candidates:
                    break
                
                t0 = time.time()
                props = _getprops_page(gp_session, gp_referer, gp_template_this, page)
                total_api_calls += 1
                total_raw_props += len(props)
                
                page_new = 0
                page_prof = 0
                page_hist = 0
                page_run_dup = 0
                
                for prop in props:
                    if candidates_processed >= args.max_new_candidates:
                        break
                    
                    lid = prop["listing_id"]
                    url = prop["url"]
                    fmt = prop["url_format"]
                    
                    # Run dedup
                    if lid in global_seen_ids or url in global_seen_urls:
                        run_duplicates += 1
                        page_run_dup += 1
                        continue
                    
                    # Check MongoDB
                    if lid in pre_tids:
                        hist_duplicates += 1
                        page_hist += 1
                        continue
                    
                    # Pre-filter professionals
                    from discovery import classify_discovery_candidate, SKIP_PROFESSIONAL
                    decision = classify_discovery_candidate(fmt)
                    if decision == SKIP_PROFESSIONAL:
                        prof_skipped += 1
                        page_prof += 1
                        global_seen_ids.add(lid)
                        global_seen_urls.add(url)
                        continue
                    
                    # New candidate - will download
                    global_seen_ids.add(lid)
                    global_seen_urls.add(url)
                    all_new_ids.append(lid)
                    
                    # Download
                    dl = None
                    for attempt in range(2):
                        try:
                            dl = download_html(url, config, attempt=attempt, session=dl_session)
                            if dl and dl.validation_status in ("INVALID", "BLOCKED") and attempt == 0:
                                if dl_session and proxy_pool:
                                    p_url, dl_session = _open_block()
                                continue
                            break
                        except Exception as e:
                            if attempt == 0 and dl_session and proxy_pool:
                                p_url, dl_session = _open_block()
                            continue
                    
                    if not dl or not dl.html:
                        download_fail += 1
                        continue
                    
                    download_ok += 1
                    
                    # Save HTML
                    save_html(url, dl.html, RUN_ID, {
                        "listing_id": lid,
                        "canonical_url": url,
                        "fetch_source": dl.fetch_source or "getprops",
                        "status_code": dl.status_code or 200,
                        "validation_status": dl.validation_status or "OK",
                    })
                    html_saved += 1
                    
                    # Recover html_path from html_store
                    actual_path = hs_html_path(url, RUN_ID)
                    html_path_str = str(actual_path) if actual_path else ""
                    html_sha = sha256_text(dl.html) if dl.html else ""
                    
                    # Extract
                    extracted = extract_listing_fields(dl.html, source_url=url)
                    if not extracted.get("images"):
                        extracted["images"] = fetch_gallery_images(dl.html, url)
                    enriched = _enrich_property_fields(extracted, url, config.uf_valor_clp, config.uf_fecha)
                    
                    # Classify
                    if classify_structural_broker(extracted):
                        classification = classify_structural_broker(extracted)
                    elif classify_structural_owner(extracted):
                        classification = classify_structural_owner(extracted)
                    else:
                        rr = classify_obvious_broker(extracted)
                        if rr:
                            classification = rr
                        else:
                            rctx = build_rule_context(extracted)
                            from classifier_rules import should_invoke_deepseek
                            desc_text = extracted.get("descripcion", extracted.get("description", ""))
                            must_invoke, ds_reason = should_invoke_deepseek(
                                rule_state="INCONCLUSIVE",
                                description=desc_text,
                                description_length=len(desc_text),
                                seller_type=str(extracted.get("seller_type", "")),
                            )
                            if not must_invoke:
                                fallbacks_used += 1
                                classification = {"state": "INCIERTO", "confidence": 0.3,
                                    "reason": f"DS skipped: {ds_reason}", "source": "rules_fallback",
                                    "deepseek_status": "NOT_NEEDED"}
                            else:
                                from deepseek_classifier import classify_with_deepseek, DeepSeekStatus as DS
                                desc_bundle = None
                                try:
                                    from deepseek_classifier import build_description_for_llm
                                    desc_bundle = build_description_for_llm(desc_text,
                                        max_chars=config.deepseek_description_max_chars)
                                except:
                                    pass
                                try:
                                    ds = classify_with_deepseek(extracted, rctx, config, desc_bundle)
                                    deepseek_calls += 1
                                    if ds and ds.status == DS.VALID.value:
                                        classification = {"state": ds.state, "confidence": ds.confidence,
                                            "reason": ds.reason, "evidence": ds.evidence, "source": "deepseek",
                                            "deepseek_raw": ds.raw, "deepseek_status": ds.status}
                                    elif ds:
                                        deepseek_errors += 1
                                        fallbacks_used += 1
                                        classification = {"state": "INCIERTO", "confidence": 0.3,
                                            "reason": f"DeepSeek {ds.status}: {ds.reason}. Fallback.",
                                        "evidence": [], "source": "rules_fallback",
                                        "deepseek_status": ds.status, "deepseek_reason": ds.reason}
                                    else:
                                        fallbacks_used += 1
                                        classification = {"state": "INCIERTO", "confidence": 0.3,
                                            "reason": "DeepSeek no result", "evidence": [],
                                            "source": "rules_fallback", "deepseek_status": DS.NOT_NEEDED.value}
                                except Exception as e:
                                    deepseek_errors += 1
                                    fallbacks_used += 1
                                    classification = {"state": "INCIERTO", "confidence": 0.3,
                                        "reason": f"DeepSeek error: {e}", "evidence": [],
                                        "source": "error", "deepseek_status": DS.API_ERROR.value}
                    
                    # Build record
                    description_length = len(enriched.get("description", enriched.get("descripcion", "")))
                    raw_record = {
                        **enriched,
                        "classification": classification,
                        "processed_at": _utcnow(),
                        "batch_id": RUN_ID,
                        "source": "owner_hunt",
                        "origen": "toctoc",
                        "source_portal": "toctoc",
                        "schema_version": "crm_v1",
                        "html_path": html_path_str,
                        "html_sha256": html_sha,
                        "description_length": description_length,
                        "description_is_truncated": description_length > config.deepseek_description_max_chars,
                    }
                    
                    # Write MongoDB
                    if args.write_db and mongo and not args.dry_run:
                        try:
                            mongo.ensure_index()
                            r = mongo.upsert_listing(raw_record)
                            if r.get("upserted_id"):
                                mongo_inserts += 1
                            else:
                                mongo_updates += 1
                        except Exception as e:
                            print(f"  MongoDB write error: {e}")
                    
                    page_new += 1
                    candidates_processed += 1
                    combo_new += 1
                
                t1 = time.time()
                print(f"  Page {page:2d}: {len(props):4d} props, {page_new:3d} new, {page_prof:3d} prof, {page_hist:3d} hist ({t1-t0:.1f}s)")
                
                if page_new == 0 and page > 1:
                    print(f"    Zero new on page {page}, stopping combo.")
                    break
            
            per_combo_metrics.append({
                "combo": combo_label,
                "pages_consumed": page,
                "candidates_in_combo": combo_new,
            })
            if combo_new > 0:
                print(f"  Combo total: {combo_new} new candidates")
    
    total_time = time.time() - total_start
    
    # Post-run snapshot
    if mongo:
        post_t = col.count_documents({"origen": "toctoc"})
        post_y = col.count_documents({"origen": "yapo"})
        post_states = {}
        for s in col.aggregate([{"$match": {"origen": "toctoc"}},
                                {"$group": {"_id": "$classification.state", "count": {"$sum": 1}}}]):
            post_states[s["_id"]] = s["count"]
        yapo_changed = post_y != pre_y
    else:
        post_t = pre_t
        post_y = pre_y
        post_states = pre_states
        yapo_changed = False
    
    print(f"\n{'='*60}")
    print(f"RUN COMPLETE")
    print(f"{'='*60}")
    print(f"  API calls: {total_api_calls}")
    print(f"  Raw props received: {total_raw_props}")
    print(f"  Professional skipped: {prof_skipped}")
    print(f"  Historical duplicates: {hist_duplicates}")
    print(f"  Run duplicates: {run_duplicates}")
    print(f"  Download OK: {download_ok}")
    print(f"  Download fail: {download_fail}")
    print(f"  Candidates processed: {candidates_processed}")
    print(f"  DeepSeek calls: {deepseek_calls}")
    print(f"  DeepSeek errors: {deepseek_errors}")
    print(f"  Fallbacks: {fallbacks_used}")
    print(f"  HTML saved: {html_saved}")
    print(f"  MongoDB inserts: {mongo_inserts}")
    print(f"  MongoDB updates: {mongo_updates}")
    print(f"  Toctoc: {pre_t} -> {post_t} (+{post_t - pre_t})")
    print(f"  Yapo: {pre_y} -> {post_y} (changed={yapo_changed})")
    print(f"  Time: {total_time:.1f}s")
    
    # Build new docs preview
    new_owners = []
    new_uncertain = []
    new_excluded = []
    if mongo and all_new_ids:
        for d in col.find({"origen": "toctoc", "listing_id": {"$in": list(set(all_new_ids))}},
                          {"_id": 0, "listing_id": 1, "url": 1, "comuna": 1, "title": 1,
                           "seller_name": 1, "seller_type": 1, "classification": 1}):
            state = d.get("classification", {}).get("state")
            rec = {
                "listing_id": d.get("listing_id"), "url": d.get("url"),
                "comuna": d.get("comuna"), "title": d.get("title"),
                "seller_name": d.get("seller_name"), "seller_type": d.get("seller_type"),
                "state": state, "reason": d.get("classification", {}).get("reason", ""),
            }
            if state == "DUEÑO_SEGURO":
                new_owners.append(rec)
            elif state == "INCIERTO":
                new_uncertain.append(rec)
            else:
                new_excluded.append(rec)
    
    # Save reports
    report = {
        "run_id": RUN_ID,
        "api_calls": total_api_calls,
        "raw_props_received": total_raw_props,
        "professional_skipped": prof_skipped,
        "historical_duplicates": hist_duplicates,
        "run_duplicates": run_duplicates,
        "download_ok": download_ok,
        "download_fail": download_fail,
        "new_unique_candidates_processed": candidates_processed,
        "deepseek_calls": deepseek_calls,
        "deepseek_errors": deepseek_errors,
        "fallbacks_used": fallbacks_used,
        "html_saved": html_saved,
        "mongo_inserts": mongo_inserts,
        "mongo_updates": mongo_updates,
        "toctoc_before": pre_t, "toctoc_after": post_t,
        "yapo_before": pre_y, "yapo_after": post_y,
        "yapo_changed": yapo_changed,
        "duration_s": round(total_time, 2),
        "per_combo_metrics": per_combo_metrics,
        "new_dueños_seguros": len(new_owners),
        "new_inciertos": len(new_uncertain),
        "new_other": len(new_excluded),
    }
    
    json.dump(report, open(OUT / "report.json", "w"), ensure_ascii=False, indent=2)
    json.dump(new_owners, open(OUT / "crm_duenos_seguros_nuevos.json", "w"), ensure_ascii=False, indent=2)
    json.dump(new_uncertain, open(OUT / "crm_inciertos_nuevos_revision.json", "w"), ensure_ascii=False, indent=2)
    
    # writes.jsonl
    with open(OUT / "writes.jsonl", "w", encoding="utf-8") as f:
        for lid in list(set(all_new_ids)):
            f.write(json.dumps({"listing_id": lid, "origen": "toctoc", "run_id": RUN_ID}, ensure_ascii=False) + "\n")
    
    # Markdown report
    md = f"""# GetProps Production Run — {RUN_ID}

## Resumen

| Métrica | Valor |
|---|---|
| API calls | {total_api_calls} |
| Props brutas recibidas | {total_raw_props} |
| Profesionales descartados | {prof_skipped} |
| Duplicados históricos | {hist_duplicates} |
| Duplicados de corrida | {run_duplicates} |
| Descargas exitosas | {download_ok} |
| Fallos de descarga | {download_fail} |
| **Candidatos nuevos procesados** | **{candidates_processed}** |
| Llamadas DeepSeek | {deepseek_calls} |
| Errores DeepSeek | {deepseek_errors} |
| Fallbacks usados | {fallbacks_used} |
| HTML guardados | {html_saved} |
| MongoDB inserts | {mongo_inserts} |
| Toctoc antes/después | {pre_t} → {post_t} |
| Yapo antes/después | {pre_y} → {post_y} |
| Yapo modificado | {yapo_changed} |
| Duración | {total_time:.1f}s |

## Nuevos DUEÑO_SEGURO: {len(new_owners)}
## Nuevos INCIERTO: {len(new_uncertain)}
## Nuevos otros: {len(new_excluded)}
"""
    open(OUT / "report.md", "w", encoding="utf-8").write(md)
    
    print(f"\nReports: {OUT}")
    print(f"  report.json, report.md, crm_duenos_seguros_nuevos.json, crm_inciertos_nuevos_revision.json, writes.jsonl")


if __name__ == "__main__":
    main()
