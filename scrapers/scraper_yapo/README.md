# Owner-hunt Yapo scraper

Este proyecto reconstruye el flujo owner-hunt de Yapo.cl dentro de la carpeta raíz `scraper/`.

## Flujo activo

- `run_owner_hunt.py`: CLI principal.
- `verify_clean_owner_hunt.py`: verificación automática de limpieza.
- `discovery.py`: discovery y paginación.
- `downloader.py`: descarga y validación HTML.
- `extractor.py`: extracción de campos del aviso.
- `classifier_rules.py`: carga centralizada de reglas activas.
- `deepseek_classifier.py`: análisis de casos no obvios.
- `mongo_store.py`: upsert con `$set`.
- `export_reports.py`: export CSV y summary.

## JSON activos

- `data/known_broker_brands.json`
- `data/hard_broker_terms.json`
- `data/company_shape_terms.json`
- `data/listing_removed_patterns.json`
- `data/search_terms.json`
- `data/owner_keywords.json`

## JSON archivados

Los JSON obsoletos quedaron fuera de la raíz `data/` en:

- `data/_deprecated_unused/known_companies.json`
- `data/_deprecated_unused/broker_keywords.json`
- `data/_deprecated_unused/negative_owner_keywords.json`

## Carpetas

- HTML: `html_dumps/`
- Cache LLM: `html_dumps/llm_cache/`
- Reportes: `reports/`
- Reglas: `data/`

## Guardado HTML

Cada aviso procesado guarda su HTML en una ruta local por batch, por ejemplo:

`html_dumps/owner_hunt_20260708_001/{fid}.html`

Mongo guarda solo `html_path`, no el HTML completo.

Los CSV también incluyen la columna `html_path`.

## Descripción para DeepSeek

La descripción completa se conserva en el extractor con metadatos de origen y truncación lógica.
DeepSeek recibe un resumen inteligente con inicio, final y fragmentos relevantes, nunca el HTML completo.

Variables configurables:

```env
DEEPSEEK_DESCRIPTION_MAX_CHARS=6000
DEEPSEEK_DESCRIPTION_HEAD_CHARS=2500
DEEPSEEK_DESCRIPTION_TAIL_CHARS=2500
DEEPSEEK_DESCRIPTION_SNIPPET_RADIUS=350
```

## Configuración DeepSeek

En `.env`:

```env
DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_ENABLED=true
DEEPSEEK_TIMEOUT_SECONDS=12
DEEPSEEK_MAX_TOKENS=500
DEEPSEEK_THINKING=false
DEEPSEEK_MAX_CALLS_PER_SESSION=0
```

Si `DEEPSEEK_MODEL` contiene `pro`, la ejecución se detiene.

## Comandos

```bash
python run_owner_hunt.py discover --start-url "https://www.yapo.cl/..." --max-pages 10 --max-urls 500
python run_owner_hunt.py discover --start-url "https://www.yapo.cl/..." --until-end --max-urls 2000
python run_owner_hunt.py process --limit 100 --write-db
python run_owner_hunt.py process --limit 100 --offset 100 --write-db
python run_owner_hunt.py run-full --start-url "https://www.yapo.cl/..." --max-pages 10 --max-urls 500 --write-db
python run_owner_hunt.py run-full --start-url "https://www.yapo.cl/..." --until-end --max-urls 2000 --write-db
python run_owner_hunt.py export --batch-id owner_hunt_20260708_001
python verify_clean_owner_hunt.py
```
