# Yapo Scraper Pipeline

## Stages

### Stage 1: Discovery
- **Script**: `scraping/scraping_yapo_proxys.py` → `discover_new_properties()`
- **Tool**: Playwright headless browser
- **Input**: Yapo listing pages (`base_url` from config)
- **Output**: List of property detail URLs
- **Guard**: `--max-pages` limits pages scraped, `max_urls_per_session` caps URLs
- **Rule**: If HTML already exists for a URL, skip (no re-scrape)

### Stage 2: Download + HTML Backup
- **Script**: `scraping/scraping_yapo_proxys.py` → `extract_fast_path()`
- **Tool**: `curl_cffi` for direct HTTP, Playwright fallback for problematic pages
- **Output**: HTML file saved to `scraping/html_dumps/{md5(url)}.html`
- **Guard**: Only downloads if URL is new or existing HTML is invalid
- **Integrity**: `html_validator.py` validates each download

### Stage 3: Offline Parsing
- **Script**: `scraping/scraping_yapo_proxys.py` → `_parse_html_fast()`
- **Input**: Local HTML file
- **Output**: Structured dict with title, price, description, seller info, images, etc.
- **Guard**: If HTML already parsed, reuses cached result (reprocess only)

### Stage 4: Classification v5
- **Script**: `scraping/scraping_yapo_proxys.py` → `classify_seller_state()`
- **Output**: Classification dict with state, scores, signals, evidence
- **States**: `CORREDOR_SEGURO` | `DUEÑO_SEGURO` | `INCIERTO` | `AD_REMOVED`
- **`AD_REMOVED`**: Anuncio borrado/eliminado por el anunciante — detected by `html_validator.py` via `LISTING_REMOVED` status (patterns: "anuncio borrado", "eliminado por el anunciante"). Set automatically in `_build_rule_based_details()` and `process_with_ai()` when `html_validation_status == LISTING_REMOVED`.
- **`scrape_stage: needs_rescrape`**: Doc needs re-scrape (old HTML invalid or missing). Used when validation fails for non-REMOVED reasons or HTML dump doesn't exist.
- **`scrape_stage: ad_removed`**: Doc confirmed as deleted ad, no re-scrape needed.
- **Enriched**: `_enrich_classification()` adds reason, evidence, version, confidence

### Stage 5: Quality Validation
- **Script**: `scraping/qa_report.py`
- **Thresholds**:
  - `classification_reason`: 100%
  - `classification_evidence`: 100%
  - `descripcion_disponible`: ≥ 90%
  - `region != N/A`: ≥ 95%
  - `tipo_propiedad != N/A`: ≥ 95%
  - `quality_score` avg: ≥ 75
  - Documents without classification: 0
  - Critical errors: 0
- **Output**: QA verdict (`PASSED_QA` or `FAILED_QA`)

### Stage 6: MongoDB Write
- **Script**: `scraping/scraping_yapo_proxys.py` → `coll.update_one()`
- **Operation**: `$set` (never `$unset`) — preserves existing fields
- **Fields**: Full details dict with classification evidence and metadata

### Stage 7: Metrics Report
- **Script**: `scraping/qa_report.py`
- **Output**: Console report + `reports/yapo_inciertos_{timestamp}.json`
- **Covers**: Distribution, field coverage, quality score, INCIERTO cases

## Key Commands

```bash
# Canary run (100 docs)
python scraping/scraping_yapo_proxys.py --max-pages 4

# Dry-run reprocess existing docs
python scraping/reprocess_parser_batch.py --dry-run

# Full reprocess existing docs
python scraping/reprocess_parser_batch.py

# QA report after scrape
python scraping/qa_report.py

# Validation cases (regression test)
python scraping/test_validation_cases_yapo.py

# Unit tests
python scraping/test_classification_v5.py
python scraping/test_classification_fixes.py
```

## Rules

1. **No re-scrape if HTML exists**: `extract_fast_path()` skips URLs with valid local HTML.
2. **No proxies unless needed**: Fast path uses `curl_cffi`; proxy fallback only on 403/429.
3. **$set only**: All MongoDB updates use `$set`, preserving existing fields.
4. **Backup before batch ops**: `backups/propiedades_captacion_{timestamp}.json` before reprocess.
5. **Deprecated scripts blocked**: `improved_scraper.py` exits with error.
6. **Distribution post-scrape (no hourly loop)**: `distribute_sourced_leads()` runs at the end of a scrape batch, not hourly on the server. Triggered via `scripts/run_distribution_after_scrape.py` from `run_toctoc.py`, `run_territorial_expansion.py`, and `run_toctoc_incremental.py`. Manual trigger: `POST /api/captacion/distribute`. Weekly SLA release stays in `captacion_sla_release_loop` (Sunday 04:00 Chile).
7. **Never redistribute managed contacts**: `distribute_sourced_leads()` and `release_stale_captaciones()` skip docs with management evidence (`has_management_evidence` in `redistribute_captacion.py`: notas, actividades, `fecha_ultima_gestion`, events in `captacion_management_events`).
