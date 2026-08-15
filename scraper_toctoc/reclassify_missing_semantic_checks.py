"""
reclassify_missing_semantic_checks.py
Reprocesa documentos Toctoc INCIERTO que nunca pasaron por DeepSeek.

Etapa 1: Reglas actualizadas (sin DeepSeek)
Etapa 2: DeepSeek v4 Flash para los que continúan INCIERTO

Uso:
  python scraper_toctoc/reclassify_missing_semantic_checks.py --stage rules --batch-size 50
  python scraper_toctoc/reclassify_missing_semantic_checks.py --stage deepseek --batch-size 25 --resume
  python scraper_toctoc/reclassify_missing_semantic_checks.py --stage all --apply
"""
import argparse, json, os, sys, time, datetime, re, unicodedata
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from chatbot.storage import get_db

# Add scraper_toctoc to path FIRST so it takes priority over root config
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)
# Also add parent for cross-module imports
sys.path.insert(0, os.path.dirname(script_dir))

REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 
                           "reports", "deepseek_missing_audit")
CHECKPOINT_FILE = os.path.join(REPORTS_DIR, "checkpoint.json")

def normalize_text(text):
    if not text: return ""
    t = str(text).strip().lower()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"[^a-z0-9+ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_CLASSIFIER_RULES_CACHE = None

def _load_classifier_rules():
    """Load classifier_rules module once and cache it."""
    global _CLASSIFIER_RULES_CACHE
    if _CLASSIFIER_RULES_CACHE is not None:
        return _CLASSIFIER_RULES_CACHE
    
    import importlib.util, importlib.machinery
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Pre-load scraper_toctoc/config.py as 'config' so classifier_rules finds it
    cfg_path = os.path.join(script_dir, "config.py")
    cfg_spec = importlib.util.spec_from_file_location("config", cfg_path)
    toctoc_cfg = importlib.util.module_from_spec(cfg_spec)
    sys.modules["config"] = toctoc_cfg
    cfg_spec.loader.exec_module(toctoc_cfg)
    
    # Now classifier_rules.py can import from config (which is scraper_toctoc/config.py)
    cr_path = os.path.join(script_dir, "classifier_rules.py")
    cr_spec = importlib.util.spec_from_file_location("classifier_rules", cr_path)
    cr = importlib.util.module_from_spec(cr_spec)
    sys.modules["classifier_rules"] = cr
    cr_spec.loader.exec_module(cr)
    
    # Restore root config module after classifier_rules is loaded
    import importlib
    root_cfg_spec = importlib.util.spec_from_file_location("root_config", 
        os.path.join(os.path.dirname(script_dir), "config.py"))
    root_cfg = importlib.util.module_from_spec(root_cfg_spec)
    sys.modules["config"] = root_cfg
    root_cfg_spec.loader.exec_module(root_cfg)
    
    _CLASSIFIER_RULES_CACHE = cr
    return cr


def classify_with_updated_rules(extracted):
    """Reimplementación de las reglas actualizadas para usar offline."""
    cr = _load_classifier_rules()
    
    classify_structural_broker = cr.classify_structural_broker
    classify_structural_owner = cr.classify_structural_owner
    classify_obvious_broker = cr.classify_obvious_broker
    should_invoke_deepseek = cr.should_invoke_deepseek
    build_semantic_check = cr.build_semantic_check
    
    # Run updated rules
    result = classify_structural_broker(extracted)
    if result:
        result["_semantic_check"] = build_semantic_check("SKIPPED_STRONG_RULE")
        return result
    
    result = classify_structural_owner(extracted)
    if result:
        result["_semantic_check"] = build_semantic_check("SKIPPED_EXPLICIT_OWNER")
        return result
    
    result = classify_obvious_broker(extracted)
    if result:
        result["_semantic_check"] = build_semantic_check("SKIPPED_STRONG_RULE")
        return result
    
    desc = extracted.get("description") or extracted.get("descripcion") or ""
    must_invoke, reason = should_invoke_deepseek(
        rule_state="INCONCLUSIVE",
        description=desc,
        description_length=len(desc),
        seller_type=str(extracted.get("seller_type", "")),
    )
    
    if must_invoke:
        return {
            "state": "INCONCLUSIVE",
            "confidence": 0.2,
            "reason": "Sin senales en reglas. Requiere DeepSeek.",
            "evidence": [],
            "source": "rules",
            "_needs_deepseek": True,
            "_semantic_check": build_semantic_check("PENDING", description_length=len(desc)),
        }
    
    return {
        "state": "INCONCLUSIVE",
        "confidence": 0.2,
        "reason": "Sin senales. " + reason,
        "evidence": [],
        "source": "rules",
        "_semantic_check": build_semantic_check("NO_DESCRIPTION"),
    }


def run_stage1_rules(dry_run=True, batch_size=50, modules=None):
    """Etapa 1: ejecutar reglas actualizadas sin DeepSeek."""
    if modules is None:
        _, _, cfg = _load_toctoc_modules()
    else:
        _, _, cfg = modules
    
    os.makedirs(REPORTS_DIR, exist_ok=True)
    db = get_db()
    coll = db[Config.CAPTACION_COLLECTION_NAME]
    
    target = list(coll.find({
        "origen": "toctoc",
        "classification.state": "INCIERTO",
        "description": {"$exists": True, "$ne": "", "$type": "string"},
        "description_length": {"$gt": 50},
    }, {
        "listing_id": 1, "title": 1, "description": 1, "description_length": 1,
        "comuna": 1, "precio_uf": 1, "dormitorios": 1, "banos": 1,
        "tipo_propiedad": 1, "operacion": 1, "seller_type": 1,
        "seller_name": 1, "url": 1, "url_format": 1, "html_path": 1,
        "classification": 1, "gestion": 1, "schema_version": 1,
        "seller_type_source": 1, "seller_type_evidence": 1,
        "publicador_visible": 1, "listing_advertiser": 1,
        "publisher_identity_candidates": 1,
    }))
    
    print(f"Etapa 1: {len(target)} documentos a procesar")
    
    results = {
        "total": len(target),
        "corredor_seguro": 0,
        "corredor_probable": 0,
        "dueno_seguro": 0,
        "requires_deepseek": 0,
        "no_description": 0,
        "errors": 0,
        "corredor_samples": [],
        "deepseek_samples": [],
    }
    
    for doc in target:
        try:
            lid = doc.get("listing_id")
            # Build extracted dict for classifier
            extracted = {
                "title": doc.get("title", ""),
                "description": doc.get("description", ""),
                "seller_type": doc.get("seller_type", ""),
                "seller_name": doc.get("seller_name", ""),
                "url": doc.get("url", ""),
                "url_format": doc.get("url_format", ""),
                "publicador_visible": doc.get("publicador_visible", doc.get("seller_name", "")),
                "listing_advertiser": doc.get("listing_advertiser", ""),
                "seller_type_source": doc.get("seller_type_source", ""),
                "seller_type_evidence": doc.get("seller_type_evidence", ""),
                "seller_jsonld_name": "",
                "contact_badges_text": "",
                "contact_name": "",
                "contact_logo_alt": "",
                "publisher_identity_candidates": doc.get("publisher_identity_candidates", []),
            }
            
            cls_result = classify_with_updated_rules(extracted)
            
            if cls_result.get("state") == "CORREDOR_SEGURO":
                results["corredor_seguro"] += 1
                if len(results["corredor_samples"]) < 20:
                    results["corredor_samples"].append({
                        "listing_id": lid,
                        "title": doc.get("title")[:80],
                        "evidence": cls_result.get("evidence", [])[:3],
                        "reason": cls_result.get("reason", "")[:120],
                        "source": cls_result.get("source", ""),
                        "prev_state": doc.get("classification", {}).get("state"),
                    })
            elif cls_result.get("state") == "CORREDOR_PROBABLE":
                results["corredor_probable"] += 1
            elif cls_result.get("state") == "DUEÑO_SEGURO":
                results["dueno_seguro"] += 1
            elif cls_result.get("_needs_deepseek"):
                results["requires_deepseek"] += 1
                if len(results["deepseek_samples"]) < 20:
                    results["deepseek_samples"].append({
                        "listing_id": lid,
                        "title": doc.get("title")[:80],
                        "desc_len": len(doc.get("description", "")),
                        "seller_type": doc.get("seller_type", ""),
                    })
            else:
                results["no_description"] += 1
        except Exception as e:
            results["errors"] += 1
            if len(results.get("errors_list", [])) < 10:
                results.setdefault("errors_list", []).append({"lid": doc.get("listing_id"), "error": str(e)})
    
    print(f"\nResultados Etapa 1:")
    print(f"  CORREDOR_SEGURO:       {results['corredor_seguro']}")
    print(f"  CORREDOR_PROBABLE:     {results['corredor_probable']}")
    print(f"  DUEÑO_SEGURO:          {results['dueno_seguro']}")
    print(f"  Requiere DeepSeek:     {results['requires_deepseek']}")
    print(f"  Errors:                {results['errors']}")
    
    # Save report
    report_path = os.path.join(REPORTS_DIR, "stage1_rules_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"Report saved: {report_path}")
    
    return results


def _load_toctoc_modules():
    """Pre-load all toctoc modules with correct config resolution."""
    import importlib.util
    script_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(script_dir)
    
    import sys as _sys
    
    # Ensure root config is loaded first and saved for restoration
    if "config" not in _sys.modules:
        root_cfg_spec = importlib.util.spec_from_file_location("config",
            os.path.join(root_dir, "config.py"))
        root_cfg_mod = importlib.util.module_from_spec(root_cfg_spec)
        _sys.modules["config"] = root_cfg_mod
        root_cfg_spec.loader.exec_module(root_cfg_mod)
    
    saved_cfg = _sys.modules["config"]
    
    # Swap config to toctoc version for module loading
    tcfg_spec = importlib.util.spec_from_file_location("config", 
        os.path.join(script_dir, "config.py"))
    tcfg_mod = importlib.util.module_from_spec(tcfg_spec)
    _sys.modules["config"] = tcfg_mod
    tcfg_spec.loader.exec_module(tcfg_mod)
    
    # classifier_rules
    cr_spec = importlib.util.spec_from_file_location("classifier_rules",
        os.path.join(script_dir, "classifier_rules.py"))
    cr_mod = importlib.util.module_from_spec(cr_spec)
    _sys.modules["classifier_rules"] = cr_mod
    cr_spec.loader.exec_module(cr_mod)
    
    # deepseek_classifier
    ds_spec = importlib.util.spec_from_file_location("deepseek_classifier",
        os.path.join(script_dir, "deepseek_classifier.py"))
    ds_mod = importlib.util.module_from_spec(ds_spec)
    _sys.modules["deepseek_classifier"] = ds_mod
    ds_spec.loader.exec_module(ds_mod)
    
    # Restore root config BEFORE calling get_config()
    _sys.modules["config"] = saved_cfg
    
    # Now get_config() can import root Config successfully
    cfg = tcfg_mod.get_config()
    
    return cr_mod, ds_mod, cfg


def run_stage2_deepseek(dry_run=True, batch_size=25, resume=False, modules=None):
    """Etapa 2: invocar DeepSeek para los que continúan INCIERTO."""
    if modules is None:
        cr_mod, ds_mod, cfg = _load_toctoc_modules()
    else:
        cr_mod, ds_mod, cfg = modules
    
    classify_with_deepseek = ds_mod.classify_with_deepseek
    DeepSeekStatus = ds_mod.DeepSeekStatus
    build_rule_context = cr_mod.build_rule_context
    build_semantic_check = cr_mod.build_semantic_check
    
    os.makedirs(REPORTS_DIR, exist_ok=True)
    db = get_db()
    coll = db[Config.CAPTACION_COLLECTION_NAME]
    
    # Load checkpoint
    processed_ids = set()
    if resume and os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            processed_ids = set(json.load(f).get("processed_ids", []))
        print(f"Resuming: {len(processed_ids)} already processed")
    
    # Get documents that need DeepSeek (stage 1 inconclusive + pending semantic check)
    if resume and processed_ids:
        # Use checkpoint listing_ids
        pass  # Will filter below
    
    target = list(coll.find({
        "origen": "toctoc",
        "classification.state": "INCIERTO",
        "description": {"$exists": True, "$ne": "", "$type": "string"},
        "description_length": {"$gt": 50},
        "$or": [
            {"classification.semantic_check.status": {"$exists": False}},
            {"classification.semantic_check.status": "PENDING"},
        ]
    }, {
        "listing_id": 1, "title": 1, "description": 1, "description_length": 1,
        "comuna": 1, "precio_uf": 1, "dormitorios": 1, "banos": 1,
        "tipo_propiedad": 1, "operacion": 1, "seller_type": 1,
        "seller_name": 1, "url": 1, "html_path": 1,
        "classification": 1, "gestion": 1,
    }))
    
    if resume and processed_ids:
        target = [d for d in target if d.get("listing_id") not in processed_ids]
    
    print(f"Etapa 2: {len(target)} documentos requieren DeepSeek")
    
    if dry_run:
        print("  DRY RUN: no se ejecutará DeepSeek realmente")
        return {"total": len(target), "dry_run": True}
    
    # Check DeepSeek is available
    if not cfg.deepseek_enabled:
        print("ERROR: DeepSeek no está habilitado en la configuración")
        print(f"  DEEPSEEK_ADJUDICATOR_ENABLED debe ser true")
        return {"error": "deepseek_disabled"}
    
    print(f"  Modelo: {cfg.deepseek_model}")
    print(f"  Timeout: {cfg.deepseek_timeout_seconds}s")
    print(f"  Max tokens: {cfg.deepseek_max_tokens}")
    
    results = {
        "total": len(target),
        "corredor_seguro": 0,
        "corredor_probable": 0,
        "dueno_seguro": 0,
        "incierto": 0,
        "errors": 0,
        "batches": 0,
        "total_tokens_in": 0,
        "total_tokens_out": 0,
        "total_duration_s": 0,
        "samples": [],
    }
    
    now_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    for i in range(0, len(target), batch_size):
        batch = target[i:i+batch_size]
        batch_start = time.time()
        batch_results = []
        
        print(f"\nBatch {i//batch_size + 1}/{(len(target)-1)//batch_size + 1} ({len(batch)} docs)")
        
        for doc in batch:
            lid = doc.get("listing_id")
            desc = doc.get("description", "")
            
            # Build extracted dict
            extracted = {
                "title": doc.get("title", ""),
                "description": desc,
                "seller_name": doc.get("seller_name", ""),
                "seller_type": doc.get("seller_type", ""),
                "publicador_visible": doc.get("seller_name", ""),
            }
            
            try:
                rctx = build_rule_context(extracted)
                ds = classify_with_deepseek(extracted, rctx, cfg)
                
                if ds and ds.status == DS.VALID.value:
                    new_state = ds.state
                    sc = build_semantic_check(
                        "VALID", model=cfg.deepseek_model,
                        prompt_version=cfg.deepseek_prompt_version,
                        description_length=len(desc), attempts=1,
                        checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                elif ds and ds.status in (DS.LEGACY_UNKNOWN.value, DS.NOT_NEEDED.value):
                    sc = build_semantic_check("ERROR", model=cfg.deepseek_model,
                        error=f"DS status: {ds.status}: {ds.reason}",
                        description_length=len(desc), attempts=1)
                    new_state = "INCIERTO"
                else:
                    sc = build_semantic_check("ERROR", model=cfg.deepseek_model,
                        error=f"DS {ds.status if ds else 'None'}: {ds.reason if ds else 'no response'}",
                        description_length=len(desc), attempts=1)
                    new_state = "INCIERTO"
                
                prev_state = doc.get("classification", {}).get("state")
                prev_final = doc.get("classification", {}).get("final_state")
                assigned_to = doc.get("gestion", {}).get("ejecutivo_asignado")
                
                # Build update
                update = {
                    "classification.state": new_state,
                    "classification.final_state": new_state,
                    "classification.confidence": ds.confidence if ds and ds.status == DS.VALID.value else 0.5,
                    "classification.confidence_source": "deepseek" if ds and ds.status == DS.VALID.value else "fallback_default",
                    "classification.state_source": "deepseek_full_description_audit",
                    "classification.reason": ds.reason if ds else "DeepSeek error",
                    "classification.evidence": ds.evidence if ds else [],
                    "classification.updated_at": datetime.datetime.now(datetime.timezone.utc),
                    "classification.semantic_check": sc,
                    "deepseek_invoked": True,
                    "deepseek_status": ds.status if ds else "ERROR",
                    "deepseek_state": ds.state if ds else "",
                    "deepseek_confidence": ds.confidence if ds else None,
                    "deepseek_reason": ds.reason if ds else "",
                    "deepseek_evidence": ds.evidence if ds else [],
                    "deepseek_model": cfg.deepseek_model,
                    "deepseek_prompt_version": cfg.deepseek_prompt_version,
                    "deepseek_description_length": len(desc),
                }
                
                # If professional conflict, remove from active pool
                if new_state in ("CORREDOR_SEGURO", "CORREDOR_PROBABLE") and assigned_to:
                    update["gestion.estado"] = "Corredor"
                    update["gestion.conflicto_profesional"] = True
                    update["gestion.fecha_conflicto"] = datetime.datetime.now(datetime.timezone.utc)
                    update["gestion.motivo_conflicto"] = "deepseek_full_description_audit"
                
                # History
                history_entry = {
                    "previous_state": prev_state,
                    "previous_final_state": prev_final,
                    "new_state": new_state,
                    "rules_result": prev_state,
                    "deepseek_result": ds.state if ds else "ERROR",
                    "deepseek_confidence": ds.confidence if ds else None,
                    "deepseek_evidence": ds.evidence if ds else [],
                    "deepseek_model": cfg.deepseek_model,
                    "description_analyzed": len(desc),
                    "assigned_to": assigned_to,
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
                update["classification.history"] = [history_entry]
                
                if not dry_run:
                    coll.update_one(
                        {"origen": "toctoc", "listing_id": lid, "classification.state": "INCIERTO"},
                        {"$set": update}
                    )
                
                # Stats
                if new_state == "CORREDOR_SEGURO":
                    results["corredor_seguro"] += 1
                elif new_state == "CORREDOR_PROBABLE":
                    results["corredor_probable"] += 1
                elif new_state == "DUEÑO_SEGURO":
                    results["dueno_seguro"] += 1
                else:
                    results["incierto"] += 1
                
                results["total_tokens_in"] += getattr(ds, 'prompt_tokens', 0) if ds else 0
                results["total_tokens_out"] += getattr(ds, 'completion_tokens', 0) if ds else 0
                
                if len(results["samples"]) < 20:
                    results["samples"].append({
                        "listing_id": lid,
                        "prev_state": prev_state,
                        "new_state": new_state,
                        "reason": (ds.reason if ds else "")[:150],
                        "evidence": (ds.evidence if ds else [])[:3],
                        "assigned_to": assigned_to,
                    })
                    
            except Exception as e:
                results["errors"] += 1
                if len(results.get("errors_list", [])) < 10:
                    results.setdefault("errors_list", []).append({"lid": lid, "error": str(e)})
        
        batch_duration = time.time() - batch_start
        results["total_duration_s"] += batch_duration
        results["batches"] += 1
        
        print(f"  Duration: {batch_duration:.1f}s")
        print(f"  Cumulative: DS={results['corredor_seguro']}, PR={results['corredor_probable']}, "
              f"OWN={results['dueno_seguro']}, INC={results['incierto']}, ERR={results['errors']}")
        
        # Save checkpoint
        new_processed = set(d.get("listing_id") for d in batch)
        processed_ids.update(new_processed)
        with open(CHECKPOINT_FILE, "w") as f:
            json.dump({"processed_ids": list(processed_ids), "results": results}, f, default=str)
    
    print(f"\nFinal resultados Etapa 2:")
    print(f"  CORREDOR_SEGURO:   {results['corredor_seguro']}")
    print(f"  CORREDOR_PROBABLE: {results['corredor_probable']}")
    print(f"  DUEÑO_SEGURO:      {results['dueno_seguro']}")
    print(f"  INCIERTO:          {results['incierto']}")
    print(f"  Errors:            {results['errors']}")
    print(f"  Tokens in:         {results['total_tokens_in']}")
    print(f"  Tokens out:        {results['total_tokens_out']}")
    print(f"  Duration:          {results['total_duration_s']:.1f}s")
    
    report_path = os.path.join(REPORTS_DIR, "stage2_deepseek_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"Report saved: {report_path}")
    
    return results


def main():
    parser = argparse.ArgumentParser(description="Reclassificar documentos INCIERTO sin DeepSeek")
    parser.add_argument("--stage", choices=["rules", "deepseek", "all"], default="rules")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true", help="Aplicar cambios (sin --dry-run)")
    parser.add_argument("--resume", action="store_true", help="Reanudar desde checkpoint")
    args = parser.parse_args()
    
    dry_run = not args.apply
    
    # Pre-load toctoc modules once
    toctoc_modules = _load_toctoc_modules()
    
    if args.stage in ("rules", "all"):
        print("\n" + "=" * 60)
        print("ETAPA 1: Reglas actualizadas")
        print("=" * 60)
        stage1 = run_stage1_rules(dry_run=dry_run, batch_size=args.batch_size, modules=toctoc_modules)
        
        if args.stage == "all":
            # Save listing_ids that need DeepSeek
            needs_ds = [s["listing_id"] for s in stage1.get("deepseek_samples", [])]
            with open(os.path.join(REPORTS_DIR, "needs_deepseek_ids.json"), "w") as f:
                json.dump(needs_ds, f)
    
    if args.stage in ("deepseek", "all"):
        print("\n" + "=" * 60)
        print(f"ETAPA 2: DeepSeek ({'DRY RUN' if dry_run else 'APPLY'})")
        print("=" * 60)
        run_stage2_deepseek(dry_run=dry_run, batch_size=args.batch_size, resume=args.resume, modules=toctoc_modules)


if __name__ == "__main__":
    main()
