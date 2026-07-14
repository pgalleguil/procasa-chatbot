from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from classifier_rules import (
    build_deepseek_context,
    build_rule_context,
    classify_obvious_broker,
    classify_structural_broker,
    classify_structural_owner,
    detect_explicit_owner,
    company_shape_in_publisher_fields,
    is_removed_listing,
)
from config import AppConfig, get_config
from crm_schema import build_crm_document
from deepseek_classifier import build_description_for_llm, classify_with_deepseek
from discovery import build_ssr_search_url, discover_listing_urls
from downloader import download_html, validate_html, html_path_for_url, DownloadResult
from html_store import save_html, load_html, load_metadata, is_valid_local_html, md5_url, metadata_path, html_path as hs_html_path
from enrich import _enrich_property_fields
from extractor import extract_listing_fields, fetch_gallery_images
from mongo_store import MongoStore
from proxy_manager import ProxyManager


def _utcnow(): return datetime.now(timezone.utc).isoformat()


def _reports_dir(config): config.ensure_layout(); return config.reports_dir
def _discovery_path(config, batch_id): return _reports_dir(config) / f"discovered_{batch_id}.json"
def _processed_path(config, batch_id): return _reports_dir(config) / f"processed_{batch_id}.json"


def _latest_discovery_file(config):
    candidates = sorted(config.reports_dir.glob("discovered_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json(path): return json.loads(path.read_text(encoding="utf-8"))


def _build_parser():
    p = argparse.ArgumentParser(description="Toctoc.com scraper")
    s = p.add_subparsers(dest="command", required=True)

    d = s.add_parser("discover", help="Discover listing URLs")
    d.add_argument("--operacion", default="venta", choices=["venta", "arriendo"])
    d.add_argument("--tipo", default="departamento")
    d.add_argument("--region", default="metropolitana")
    d.add_argument("--comuna", default="la-florida")
    d.add_argument("--estado", type=int, default=None, help="Estado: 0=todos 1=nuevo 2=usado")
    d.add_argument("--publicador", type=int, default=None, help="Publicador: 0=todos 1=profesional 2=particular")
    d.add_argument("--precio-desde", type=int, default=None, help="Precio minimo en CLP")
    d.add_argument("--precio-hasta", type=int, default=None, help="Precio maximo en CLP")
    d.add_argument("--max-pages", type=int, default=3)
    d.add_argument("--max-urls", type=int, default=200)
    d.add_argument("--batch-id", default="")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--use-playwright-discovery", action="store_true", help="Use Playwright for pagination")
    d.add_argument("--proxy-mode", choices=["direct", "proxy", "auto"], default=None)

    pr = s.add_parser("process", help="Process discovered HTML")
    pr.add_argument("--batch-id", default="")
    pr.add_argument("--limit", type=int, default=50)
    pr.add_argument("--offset", type=int, default=0)
    pr.add_argument("--write-db", action="store_true")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--no-llm", action="store_true")
    pr.add_argument("--use-playwright", action="store_true")
    pr.add_argument("--proxy-mode", choices=["direct", "proxy", "auto"], default=None)
    pr.add_argument("--max-proxy-mb", type=float, default=0, help="Max MB of proxy traffic before stopping")
    pr.add_argument("--reuse-html", action="store_true", help="Reuse local HTML if valid")
    pr.add_argument("--force-download", action="store_true", help="Force re-download even if HTML exists")
    pr.add_argument("--reprocess-html-only", action="store_true", help="Process only local HTML, no network")
    pr.add_argument("--reprocess-existing", action="store_true", help="Allow reprocessing of existing MongoDB docs")

    rf = s.add_parser("run-full", help="Discover + process in one step")
    rf.add_argument("--operacion", default="venta", choices=["venta", "arriendo"])
    rf.add_argument("--tipo", default="departamento")
    rf.add_argument("--region", default="metropolitana")
    rf.add_argument("--comuna", default="la-florida")
    rf.add_argument("--estado", type=int, default=None, help="Estado: 0=todos 1=nuevo 2=usado")
    rf.add_argument("--publicador", type=int, default=None, help="Publicador: 0=todos 1=profesional 2=particular")
    rf.add_argument("--precio-desde", type=int, default=None, help="Precio minimo en CLP")
    rf.add_argument("--precio-hasta", type=int, default=None, help="Precio maximo en CLP")
    rf.add_argument("--max-pages", type=int, default=3)
    rf.add_argument("--max-urls", type=int, default=200)
    rf.add_argument("--limit", type=int, default=50)
    rf.add_argument("--write-db", action="store_true")
    rf.add_argument("--dry-run", action="store_true")
    rf.add_argument("--no-llm", action="store_true")
    rf.add_argument("--use-playwright", action="store_true")
    rf.add_argument("--use-playwright-discovery", action="store_true")
    rf.add_argument("--proxy-mode", choices=["direct", "proxy", "auto"], default=None)
    rf.add_argument("--max-proxy-mb", type=float, default=0, help="Max MB of proxy traffic before stopping")
    rf.add_argument("--reuse-html", action="store_true", help="Reuse local HTML if valid")
    rf.add_argument("--force-download", action="store_true", help="Force re-download even if HTML exists")
    rf.add_argument("--reprocess-html-only", action="store_true", help="Process only local HTML, no network")
    rf.add_argument("--reprocess-existing", action="store_true", help="Allow reprocessing of existing MongoDB docs")

    return p


def cmd_discover(args, config):
    from proxy_manager import ProxyManager
    batch_id = args.batch_id or config.generate_batch_id()
    print(f"Batch: {batch_id}")
    
    # Proxy setup
    proxy_manager = None
    proxy_mode = (args.proxy_mode or config.proxy_mode or "direct").lower()
    if proxy_mode == "proxy":
        proxy_manager = ProxyManager.from_env()
        if not proxy_manager.has_proxies():
            raise RuntimeError(
                "proxy_mode=proxy but no proxies configured. "
                "Set PROXIES, PROXY_URLS, or TOCTOC_PROXY_URLS environment variables.")
        p = proxy_manager.get_current_proxy()
        safe = p.safe_url if p else "N/A"
        host = p.host_port if p else "N/A"
        print(f"  Proxy mode: {proxy_mode}, proxy={safe}, host={host}")
    else:
        proxy_manager = ProxyManager()
        print(f"  Proxy mode: {proxy_mode} (proxy_applied=false)")
    
    discovered = discover_listing_urls(
        batch_id=batch_id,
        use_playwright=args.use_playwright_discovery,
        max_pages=args.max_pages,
        max_urls=args.max_urls,
        operacion=args.operacion,
        tipo=args.tipo,
        region=args.region,
        comuna=args.comuna,
        estado=getattr(args, "estado", None),
        publicador=getattr(args, "publicador", None),
        precio_desde=getattr(args, "precio_desde", None),
        precio_hasta=getattr(args, "precio_hasta", None),
        proxy_manager=proxy_manager,
    )
    print(f"Discovered {len(discovered)} URLs")
    if not args.dry_run:
        _save_json(_discovery_path(config, batch_id), discovered)
        print(f"Saved to {_discovery_path(config, batch_id)}")
    else:
        for item in discovered[:10]:
            print(f"  p{item.get('discovery_page','?')} [{item.get('listing_id','')}] {item['url']}")


def cmd_process(args, config):
    import time as _time
    batch_id = args.batch_id
    if not batch_id:
        latest = _latest_discovery_file(config)
        if latest:
            batch_id = latest.stem.replace("discovered_", "")
            print(f"Using latest batch: {batch_id}")
        else:
            print("No batch specified and no previous discovery found.")
            return
    discovery_file = _discovery_path(config, batch_id)
    if not discovery_file.exists():
        print(f"Discovery file not found: {discovery_file}")
        return
    discovered = _load_json(discovery_file)
    batch = discovered[args.offset:args.offset + args.limit]
    print(f"Processing {len(batch)} records (offset={args.offset}, limit={args.limit})")
    mongo = MongoStore(config) if args.write_db else None
    processed: list[dict] = []
    all_dls: list[tuple[dict, DownloadResult]] = []
    _pw = None

    proxy_mode = (args.proxy_mode or config.proxy_mode or "direct").lower()
    from proxy_manager import load_proxies_from_env
    proxy_pool = load_proxies_from_env()
    print(f"  Proxy mode: {proxy_mode}, Pool: {len(proxy_pool)} proxies")

    # ---- PRE-FILTER: skip professional formats before download ----
    from discovery import classify_discovery_candidate, SKIP_PROFESSIONAL, KEEP_OWNER_CANDIDATE, KEEP_AMBIGUOUS
    from discovery import classify_url_format as _cfmt
    pre_filter_results = {"skipped": 0, "kept": 0, "ambiguous": 0}
    filtered_batch = []
    for item in batch:
        url_format = item.get("url_format") or _cfmt(item.get("url", ""))
        decision = classify_discovery_candidate(url_format)
        item["discovery_decision"] = decision
        item["discovery_decision_reason"] = f"url_format={url_format}"
        if decision == SKIP_PROFESSIONAL:
            pre_filter_results["skipped"] += 1
            # Still add to processed as skipped (no download)
            processed.append({**item,
                "classification": {"state": "INCIERTO", "confidence": 0.3,
                    "reason": f"Discovery pre-filter: {decision} (formato profesional: {url_format})",
                    "evidence": [f"url_format={url_format}"],
                    "source": "discovery_pre_filter"},
                "processed_at": _utcnow(), "batch_id": batch_id,
                "source": "owner_hunt", "origen": "toctoc", "source_portal": "toctoc",
                "schema_version": "crm_v1",
                "skip_reason": "PROFESSIONAL_URL_FORMAT",
                "scrape_stage": "skipped_by_pre_filter",
            })
        elif decision == KEEP_OWNER_CANDIDATE:
            pre_filter_results["kept"] += 1
            filtered_batch.append(item)
        else:  # KEEP_AMBIGUOUS
            pre_filter_results["ambiguous"] += 1
            filtered_batch.append(item)
    print(f"\n  Pre-filter: {pre_filter_results['kept']} kept, {pre_filter_results['skipped']} skipped (professional), {pre_filter_results['ambiguous']} ambiguous")
    batch = filtered_batch
    if not batch:
        print("  No candidates remaining after pre-filter. Skipping download phase.")
        _save_json(_processed_path(config, batch_id), processed)
        print(f"\nSaved {len(processed)} records to {_processed_path(config, batch_id)}")
        return

    # ---- HISTORICAL DEDUP: skip existing MongoDB docs (unless --reprocess-existing) ----
    reprocess_existing = getattr(args, 'reprocess_existing', False)
    historical_duplicates = 0
    if not reprocess_existing and mongo is not None:
        dedup_batch = []
        for item in batch:
            lid = str(item.get("listing_id", "")).strip()
            if not lid:
                dedup_batch.append(item)
                continue
            existing = mongo.collection().find_one(
                {"origen": "toctoc", "listing_id": lid},
                {"_id": 1}
            )
            if existing:
                historical_duplicates += 1
                processed.append({**item,
                    "classification": {"state": "INCIERTO", "confidence": 0.3,
                        "reason": "historical_duplicate: ya existia en MongoDB",
                        "evidence": [f"listing_id={lid}"],
                        "source": "dedup_pre_filter"},
                    "processed_at": _utcnow(), "batch_id": batch_id,
                    "source": "owner_hunt", "origen": "toctoc", "source_portal": "toctoc",
                    "schema_version": "crm_v1",
                    "skip_reason": "HISTORICAL_DUPLICATE",
                    "scrape_stage": "skipped_by_dedup",
                })
            else:
                dedup_batch.append(item)
        batch = dedup_batch
        print(f"  Historical dedup: {historical_duplicates} skipped (already in MongoDB), {len(batch)} remaining")
    elif reprocess_existing:
        print(f"  --reprocess-existing: allowing reprocessing of {len(batch)} docs")
    else:
        print(f"  No MongoDB connection for dedup ({len(batch)} candidates proceed)")

    if not batch:
        print("  No candidates after historical dedup. Skipping download phase.")
        _save_json(_processed_path(config, batch_id), processed)
        print(f"\nSaved {len(processed)} records to {_processed_path(config, batch_id)}")
        return

    # ---- TIMING ----
    batch_start = _time.time()
    proxy_sessions_timings = []
    current_session = {"id": "", "start": 0, "end": 0, "first_req": 0, "last_req": 0, "count": 0, "reason": ""}
    bytes_downloaded = 0
    proxy_bytes = {"total": 0, "success": 0, "failed": 0, "retry": 0, "playwright": 0, "saved_by_reuse": 0}
    traffic_limit_bytes = int(args.max_proxy_mb * 1_000_000) if args.max_proxy_mb > 0 else 0
    traffic_limit_reached = False
    html_reuse_count = 0
    html_download_count = 0

    # ---- PHASE 1: DOWNLOAD (proxy active) ----
    print(f"\n{'='*60}")
    print(f"FASE A: DESCARGA (proxy activo)")
    print(f"{'='*60}")

    def _ensure_pw(p_url=None):
        nonlocal _pw
        if _pw is None:
            try:
                from playwright.sync_api import sync_playwright as _sp
                pw_config = {"server": p_url} if p_url else None
                pm = _sp()
                pw = pm.__enter__()
                br = pw.chromium.launch(headless=True, proxy=pw_config)
                ctx = br.new_context(user_agent=config.user_agent, viewport={"width": 1920, "height": 1080}, locale="es-CL")
                _pw = {"pm": pm, "br": br, "pg": ctx.new_page()}
            except Exception as e:
                print(f"  Playwright init failed: {e}")
                return False
        return True

    def _close_pw():
        nonlocal _pw
        if _pw:
            try:
                if _pw.get("br"): _pw["br"].close()
                _pw.get("pm", type("x",(),{"__exit__":lambda s,*a:None})()).__exit__(None,None,None)
            except: pass
            _pw = None

    def _download_with_pw(url, hpath, p_url=None):
        if not _ensure_pw(p_url):
            raise RuntimeError("Playwright not available")
        pg = _pw["pg"]
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(3000)
            try: pg.wait_for_selector(".info-anunciante, .cf-contacto, [class*='contacto']", timeout=8000)
            except: pass
            pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            pg.wait_for_timeout(1500)
            pg.evaluate("window.scrollTo(0, 0)")
            pg.wait_for_timeout(500)
            html = pg.content()
            hpath.parent.mkdir(parents=True, exist_ok=True)
            hpath.write_text(html, encoding="utf-8")
            v = validate_html(html)
            return DownloadResult(url=url, html=html, status_code=200, fetch_source="playwright",
                html_path=hpath, validation_status=v["status"], validation_reason=v["reason"], blocked=v["status"]=="BLOCKED")
        except Exception as e:
            return DownloadResult(url=url, html="", status_code=None, fetch_source="playwright_error",
                html_path=hpath, validation_status="INVALID", validation_reason=str(e), blocked=False)

    if proxy_mode == "proxy":
        block = {"proxy": None, "count": 0, "max": 25, "idx": 0}
        session = None

        def _open_block():
            nonlocal session
            if session:
                try: session.close()
                except: pass
                current_session["end"] = _time.time()
                proxy_sessions_timings.append(dict(current_session))
            if not proxy_pool:
                return None, None
            block["idx"] = (block["idx"] + 1) % len(proxy_pool)
            p_url = proxy_pool[block["idx"]]
            block["proxy"] = p_url
            block["count"] = 0
            import requests as req
            session = req.Session()
            session.headers.update({"Accept-Encoding": "gzip, deflate, br"})
            t = _time.time()
            current_session.update({"id": f"blk_{block['idx']:02d}", "start": t, "first_req": 0, "last_req": 0, "count": 0, "reason": ""})
            return p_url, session

        p_url, session = _open_block() if proxy_pool else (None, None)

        for i, item in enumerate(batch):
            url = item["url"]
            if block["count"] >= block["max"] and session:
                current_session["reason"] = "block_limit"
                p_url, session = _open_block()

            dl = None
            attempt_bytes = 0

            # Check for reusable local HTML
            html_reused = False
            if args.reuse_html and not args.force_download and not args.reprocess_html_only:
                valid, status = is_valid_local_html(url, batch_id)
                if valid:
                    local_html = load_html(url, batch_id)
                    if local_html:
                        dl = DownloadResult(url=url, html=local_html, status_code=200, fetch_source="local_cache",
                            html_path=hs_html_path(url, batch_id), validation_status=status,
                            validation_reason="local_html_reuse", blocked=False)
                        html_reuse_count += 1
                        proxy_bytes["saved_by_reuse"] += 1
                        html_reused = True

            if not html_reused and not args.reprocess_html_only:
                for attempt in range(3):
                    if traffic_limit_reached:
                        print(f"  Traffic limit reached, stopping.")
                        break
                    try:
                        dl = download_html(url, config, batch_id=batch_id, attempt=1, session=session)
                        if dl:
                            attempt_bytes = dl.wire_bytes
                            proxy_bytes["total"] += dl.wire_bytes
                            if dl.validation_status == "BLOCKED":
                                proxy_bytes["retry"] += dl.wire_bytes
                                current_session["reason"] = "blocked"
                                p_url, session = _open_block()
                                continue
                            if dl.validation_status == "INVALID":
                                proxy_bytes["retry"] += dl.wire_bytes
                        break
                    except Exception as e:
                        err = str(e).lower()
                        if any(k in err for k in ["proxy", "connection", "timeout", "prematurely", "reset", "remote end", "403", "forbidden", "429"]):
                            proxy_bytes["retry"] += attempt_bytes or 100000
                            current_session["reason"] = "connection_error"
                            p_url, session = _open_block()
                            continue
                        print(f"  Proxy error, trying Playwright...")
                        dl = _download_with_pw(url, html_path_for_url(url, config, batch_id=batch_id), p_url)
                        if dl and dl.html:
                            proxy_bytes["playwright"] += len(dl.html)
                        if not dl or not dl.html:
                            proxy_bytes["retry"] += attempt_bytes or 100000
                            current_session["reason"] = "playwright_failed"
                            p_url, session = _open_block()
                            continue
                        break

            # Save HTML + metadata
            if dl and dl.html:
                meta = {
                    "listing_id": item.get("listing_id", ""),
                    "canonical_url": url,
                    "fetch_source": dl.fetch_source,
                    "status_code": dl.status_code or 200,
                    "content_encoding": getattr(dl, "content_encoding", ""),
                    "wire_bytes": dl.wire_bytes,
                    "validation_status": dl.validation_status,
                    "proxy_used": dl.proxy_used,
                    "proxy_session_id": current_session.get("id", "") if dl.proxy_used else "",
                }
                save_html(url, dl.html, batch_id, meta)
                # Recover actual path from html_store after save
                from html_store import html_path as _hp, sha256_text as _st
                actual_path = _hp(url, batch_id)
                if actual_path:
                    dl._html_path_proxy = str(actual_path)
                    dl._html_sha256_proxy = _st(dl.html)
                html_download_count += 1

            # Check traffic limit
            if traffic_limit_bytes > 0 and proxy_bytes["total"] >= traffic_limit_bytes:
                traffic_limit_reached = True
                print(f"  TRAFFIC LIMIT REACHED: {proxy_bytes['total']/1_000_000:.1f} MB / {args.max_proxy_mb} MB")

            if dl and dl.html:
                if not html_reused:
                    block["count"] += 1
                    if current_session["first_req"] == 0:
                        current_session["first_req"] = _time.time()
                    current_session["last_req"] = _time.time()
                    current_session["count"] += 1
                    proxy_bytes["success"] += dl.wire_bytes
                    bytes_downloaded += len(dl.html)
            else:
                proxy_bytes["failed"] += attempt_bytes
                print(f"  Failed to download.")
                dl = DownloadResult(url=url, html="", status_code=None, fetch_source="failed",
                    html_path=html_path_for_url(url, config, batch_id=batch_id),
                    validation_status="INVALID", validation_reason="all_attempts_failed", blocked=False)

            all_dls.append((item, dl))
            if (i+1) % 10 == 0 or (i+1) == len(batch):
                print(f"  Descargadas {i+1}/{len(batch)} ({block['count']} en bloque actual)")

        # Close last session
        if session:
            try: session.close()
            except: pass
            current_session["end"] = _time.time()
            proxy_sessions_timings.append(dict(current_session))

        _close_pw()
        download_end = _time.time()
        print(f"  Descarga completada en {download_end - batch_start:.1f}s")

    else:
        # Direct/auto mode: download one by one
        for i, item in enumerate(batch):
            url = item["url"]
            dl = None
            for attempt in range(3):
                if attempt == 0:
                    try: dl = download_html(url, config, batch_id=batch_id, attempt=0)
                    except: continue
                elif attempt == 1:
                    print(f"  Trying Playwright...")
                    dl = _download_with_pw(url, html_path_for_url(url, config, batch_id=batch_id))
                else:
                    p_url = proxy_pool[0] if (proxy_mode == "auto" and proxy_pool) else None
                    try: dl = download_html(url, config, batch_id=batch_id, attempt=1 if p_url else 0)
                    except: continue
                if dl and dl.validation_status in ("INVALID", "BLOCKED") and dl.fetch_source != "playwright_error":
                    continue
                break
            if dl and dl.html: bytes_downloaded += len(dl.html)
            all_dls.append((item, dl or DownloadResult(url=url, html="", status_code=None, fetch_source="failed",
                html_path=html_path_for_url(url, config, batch_id=batch_id), validation_status="INVALID", validation_reason="all_failed", blocked=False)))
        _close_pw()
        download_end = _time.time()

    # ---- PHASE 2: PROCESS (proxy closed) ----
    print(f"\n{'='*60}")
    print(f"FASE B: PROCESAMIENTO (proxy cerrado)")
    print(f"{'='*60}")

    for i, (item, dl) in enumerate(all_dls):
        url = item.get("url", "")
        print(f"[{i+1}/{len(all_dls)}] {url[:80]}...")

        if dl.validation_status in ("INVALID", "BLOCKED") or not dl.html:
            print(f"  HTML no disponible ({dl.validation_status}), guardando registro basico.")
            raw_record = {**item, "html_validation_status": dl.validation_status,
                         "html_validation_reason": dl.validation_reason, "fetch_source": dl.fetch_source,
                         "classification": {"state": "AD_REMOVED" if dl.validation_status == "LISTING_REMOVED" else "INCIERTO",
                                           "confidence": 0.3, "reason": f"Download failed: {dl.validation_reason}",
                                           "source": "download_fallback"},
                         "processed_at": _utcnow(), "batch_id": batch_id, "source": "owner_hunt",
                         "origen": "toctoc", "source_portal": "toctoc", "schema_version": "crm_v1"}
            processed.append(raw_record)
            continue

        extracted = extract_listing_fields(dl.html, source_url=url)
        if not extracted.get("images"):
            extracted["images"] = fetch_gallery_images(dl.html, url)
        extracted["html_validation_status"] = dl.validation_status
        extracted["html_validation_reason"] = dl.validation_reason
        extracted["fetch_source"] = dl.fetch_source
        enriched = _enrich_property_fields(extracted, url, config.uf_valor_clp, config.uf_fecha)

        rule_result = classify_structural_broker(extracted) or classify_structural_owner(extracted) or classify_obvious_broker(extracted)
        rule_state = (rule_result or {}).get("state", "INCONCLUSIVE")
        if rule_result:
            classification = rule_result
        else:
            rr = None
            if rr:
                classification = rr
            else:
                rctx = build_rule_context(extracted)
                if args.no_llm:
                    classification = {"state": "INCONCLUSIVE", "confidence": 0.3,
                        "reason": "No se encontraron senales suficientes de corredor ni de propietario.",
                        "evidence": rctx.get("company_like_evidence", []), "source": "rules_json",
                        "ai_not_used_reason": "DeepSeek disabled via --no-llm",
                        **company_shape_in_publisher_fields(extracted)}
                else:
                    from classifier_rules import should_invoke_deepseek
                    desc = extracted.get("descripcion", extracted.get("description", ""))
                    must_invoke, ds_reason = should_invoke_deepseek(
                        rule_state="INCONCLUSIVE",
                        description=desc,
                        description_length=len(desc),
                        seller_type=str(extracted.get("seller_type", "")),
                    )
                    if not must_invoke:
                        classification = {"state": "INCIERTO", "confidence": 0.3,
                            "reason": f"DS skipped: {ds_reason}", "source": "rules_fallback",
                            "deepseek_status": "NOT_NEEDED"}
                    else:
                        from deepseek_classifier import DeepSeekStatus as DSStatus
                        db = build_description_for_llm(desc,
                            max_chars=config.deepseek_description_max_chars)
                        try:
                            ds = classify_with_deepseek(extracted, rctx, config, db)
                            if ds and ds.status == DSStatus.VALID.value:
                                final_state = ds.state
                                final_confidence = ds.confidence
                                final_reason = ds.reason
                                post_validation = ""
                                if str(ds.state).startswith("DUE") and not detect_explicit_owner(extracted):
                                    final_state = "INCIERTO"
                                    final_confidence = min(float(ds.confidence), 0.6)
                                    final_reason = "Validación posterior: la frase sobre dueño está en tercera persona y no identifica al publicador. " + ds.reason
                                    post_validation = "third_person_owner_phrase_downgraded"
                                classification = {"state": final_state, "confidence": final_confidence, "reason": final_reason,
                                    "evidence": ds.evidence, "source": "deepseek", "deepseek_raw": ds.raw,
                                    "deepseek_status": ds.status, "deepseek_payload": ds.payload,
                                    "deepseek_message_content": ds.message_content,
                                    "deepseek_reasoning_content": ds.reasoning_content,
                                    "post_validation": post_validation, "deepseek_proposed_state": ds.state}
                            elif ds:
                                rule_state = rctx.get("owner_signal_evidence", []) and "INCONCLUSIVE" or "INCONCLUSIVE"
                                classification = {"state": "INCONCLUSIVE", "confidence": 0.3,
                                    "reason": f"DeepSeek {ds.status}: {ds.reason}. Fallback a reglas.",
                                    "evidence": rctx.get("company_like_evidence", []), "source": "rules_json",
                                    "deepseek_status": ds.status, "deepseek_reason": ds.reason,
                                    **company_shape_in_publisher_fields(extracted)}
                            else:
                                classification = {"state": "INCONCLUSIVE", "confidence": 0.3, "reason": "DeepSeek no result",
                                    "evidence": [], "source": "fallback", "deepseek_status": DSStatus.NOT_NEEDED.value}
                        except Exception as es:
                            classification = {"state": "INCONCLUSIVE", "confidence": 0.3, "reason": f"DeepSeek error: {es}",
                                "evidence": [], "source": "error", "deepseek_status": DSStatus.API_ERROR.value}

        classification["rule_state"] = rule_state
        classification["final_state"] = classification.get("state")
        classification["rules_version"] = "toctoc-owner-rules-v2"
        classification["prompt_version"] = "toctoc-deepseek-owner-v2"
        classification["analysis_at"] = _utcnow()
        classification["trace"] = {
            "rule_state": rule_state, "final_state": classification.get("state"),
            "deepseek_payload": classification.get("deepseek_payload", {}),
            "deepseek_message_content": classification.get("deepseek_message_content", ""),
            "deepseek_reasoning_content": classification.get("deepseek_reasoning_content", ""),
            "deepseek_raw": classification.get("deepseek_raw", {}),
            "rules_version": classification["rules_version"],
            "prompt_version": classification["prompt_version"],
            "analysis_at": classification["analysis_at"],
        }

        # HTML tracing
        from html_store import html_path as _hp, sha256_text as _st
        
        # Compute html_path based on the actual saved file location
        # Priority: proxy-recovered > downloader path > html_store path
        html_dump_path = ""
        html_sha256 = ""
        
        proxy_html_path = getattr(dl, '_html_path_proxy', None) or getattr(dl, '_html_sha256_proxy', None)
        if proxy_html_path:
            html_dump_path = str(getattr(dl, '_html_path_proxy', ''))
            html_sha256 = str(getattr(dl, '_html_sha256_proxy', ''))
        elif hasattr(dl, 'html_path') and dl.html_path:
            html_dump_path = str(dl.html_path)
        elif url and batch_id:
            try:
                actual = _hp(url, batch_id)
                if actual:
                    html_dump_path = str(actual)
            except Exception:
                html_dump_path = ""
        
        if not html_sha256 and dl and dl.html:
            html_sha256 = _st(dl.html)
        description_length = len(enriched.get("description", enriched.get("descripcion", "")))
        description_is_truncated = description_length > config.deepseek_description_max_chars

        raw_record = {**enriched, **{k: v for k, v in item.items() if v not in (None, "", "N/A", "DESCONOCIDO")},
            "classification": classification, "processed_at": _utcnow(), "batch_id": batch_id,
            "source": "owner_hunt", "origen": "toctoc", "source_portal": "toctoc", "schema_version": "crm_v1",
            "html_path": html_dump_path, "html_sha256": html_sha256,
            "description_length": description_length,
            "description_is_truncated": description_is_truncated}
        processed.append(raw_record)

        if args.write_db and mongo and not args.dry_run:
            try:
                mongo.ensure_index(); mongo.connect()
                r = mongo.upsert_listing(raw_record)
                action = "inserted" if r["upserted_id"] else "updated"
                print(f"  MongoDB: {action} (matched={r['matched_count']}, modified={r['modified_count']})")
            except Exception as e:
                print(f"  MongoDB write failed: {e}")

    # ---- REPORT ----
    _save_json(_processed_path(config, batch_id), processed)
    total_time = _time.time() - batch_start
    download_time = download_end - batch_start
    process_time = total_time - download_time

    MB = 1_000_000
    print(f"\n{'='*60}")
    print(f"MÉTRICAS DE TRÁFICO (WIRE BYTES)")
    print(f"{'='*60}")
    print(f"wire_bytes_total: {proxy_bytes['total']:,} ({proxy_bytes['total']/MB:.2f} MB)")
    print(f"wire_mb_total: {proxy_bytes['total']/MB:.2f}")
    print(f"wire_gb_total: {proxy_bytes['total']/MB/1000:.4f}")
    print(f"successful_wire_mb: {proxy_bytes['success']/MB:.2f}")
    print(f"failed_wire_mb: {proxy_bytes['failed']/MB:.2f}")
    print(f"retry_wire_mb: {proxy_bytes['retry']/MB:.2f}")
    print(f"playwright_wire_mb: {proxy_bytes['playwright']/MB:.2f}")
    print(f"proxy_bytes_saved_by_reuse: {proxy_bytes['saved_by_reuse']}")
    if args.reuse_html:
        print(f"html_reuse_count: {html_reuse_count}")
        print(f"html_download_count: {html_download_count}")
        print(f"proxy_mb_saved_by_reuse: {(html_reuse_count * (proxy_bytes['total']/max(1,html_download_count)))/MB:.2f}" if html_download_count > 0 else "")
    print(f"average_wire_mb_per_property: {proxy_bytes['total']/MB/len(batch):.4f}" if batch else "")
    print(f"traffic_limit: {args.max_proxy_mb} MB {'(no limit)' if args.max_proxy_mb==0 else ''}")
    print(f"traffic_limit_reached: {traffic_limit_reached}")

    # Estimate disk usage
    avg_html = bytes_downloaded / max(html_download_count, 1) if html_download_count else 600000
    print(f"\n{'='*60}")
    print(f"ALMACENAMIENTO HTML")
    print(f"{'='*60}")
    print(f"html_files_created: {html_download_count}")
    print(f"html_reused: {html_reuse_count}")
    print(f"average_html_bytes: {avg_html:,.0f} ({avg_html/1024/1024:.2f} MB)")
    print(f"estimated_disk_1061: {avg_html * 1061 / 1024 / 1024:.0f} MB")

    if proxy_mode == "proxy" and proxy_sessions_timings:
        print(f"\n{'='*60}")
        print(f"DETALLE POR BLOQUE")
        print(f"{'='*60}")
        cumul = 0
        for s in proxy_sessions_timings:
            act = s.get("last_req",0) - s.get("first_req",0) if s.get("first_req") else 0
            idle = max(0, s.get("end",0) - s.get("start",0) - act)
            cumul += s.get("count",0)
            print(f"  {s['id']:8s}: {s['count']:3d} props, activo={act:.1f}s, idle={idle:.1f}s, razon={s.get('reason','')}")
        
        print(f"\n{'='*60}")
        print(f"TIEMPOS")
        print(f"{'='*60}")
        print(f"batch_total_duration: {total_time:.1f}s")
        print(f"download_phase (Fase A): {download_time:.1f}s")
        print(f"process_phase (Fase B): {process_time:.1f}s")

    print(f"\nProcessed {len(processed)} records -> {_processed_path(config, batch_id)}")


def cmd_run_full(args, config):
    from proxy_manager import ProxyManager
    batch_id = config.generate_batch_id()
    print(f"Batch: {batch_id}")
    
    proxy_manager = None
    proxy_mode = (args.proxy_mode or config.proxy_mode or "direct").lower()
    if proxy_mode == "proxy":
        proxy_manager = ProxyManager.from_env()
        if not proxy_manager.has_proxies():
            raise RuntimeError(
                "proxy_mode=proxy but no proxies configured. "
                "Set PROXIES, PROXY_URLS, or TOCTOC_PROXY_URLS environment variables.")
        p = proxy_manager.get_current_proxy()
        print(f"  Proxy mode: {proxy_mode}, proxy={p.safe_url if p else 'N/A'}")
    else:
        proxy_manager = ProxyManager()
        print(f"  Proxy mode: {proxy_mode} (proxy_applied=false)")
    
    discovered = discover_listing_urls(
        batch_id=batch_id,
        use_playwright=args.use_playwright_discovery,
        max_pages=args.max_pages,
        max_urls=args.max_urls,
        operacion=args.operacion,
        tipo=args.tipo,
        region=args.region,
        comuna=args.comuna,
        estado=getattr(args, "estado", None),
        publicador=getattr(args, "publicador", None),
        precio_desde=getattr(args, "precio_desde", None),
        precio_hasta=getattr(args, "precio_hasta", None),
        proxy_manager=proxy_manager,
    )
    print(f"Discovered {len(discovered)} URLs")
    _save_json(_discovery_path(config, batch_id), discovered)
    ca = argparse.Namespace(
        batch_id=batch_id,
        limit=args.limit or len(discovered),
        offset=0,
        write_db=args.write_db,
        dry_run=args.dry_run,
        no_llm=args.no_llm,
        use_playwright=args.use_playwright,
        proxy_mode=args.proxy_mode,
        max_proxy_mb=args.max_proxy_mb,
        reuse_html=args.reuse_html,
        force_download=args.force_download,
        reprocess_html_only=args.reprocess_html_only,
        reprocess_existing=getattr(args, "reprocess_existing", False),
    )
    cmd_process(ca, config)


def main():
    config = get_config()
    args = _build_parser().parse_args()
    dispatcher = {"discover": cmd_discover, "process": cmd_process, "run-full": cmd_run_full}
    dispatcher[args.command](args, config)


if __name__ == "__main__":
    main()
