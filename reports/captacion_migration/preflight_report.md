# Preflight Report — Migración `yapo_propiedades` → `propiedades_captacion`

## 1. Colecciones en `URLS`

- `yapo_propiedades` ✓ existe
- `propiedades_captacion` ✗ no existe → rename posible sin `dropTarget`

## 2. Conteo de documentos

| Métrica | Valor |
|---------|-------|
| Total documentos | 7.519 |
| `origen=toctoc` | 2.403 |
| `origen=yapo` | 5.116 |
| Sin origen | 0 |

## 3. Distribución Toctoc por `classification.state`

| Estado | Cantidad |
|--------|----------|
| INCIERTO | 1.507 |
| CORREDOR_SEGURO | 585 |
| CORREDOR_PROBABLE | 219 |
| DUEÑO_SEGURO | 91 |
| AD_REMOVED | 1 |
| **Total Toctoc** | **2.403** |

## 4. Asignaciones actuales

| Métrica | Valor |
|---------|-------|
| Toctoc con `gestion.ejecutivo_id` | 0 |
| Toctoc sin `gestion.ejecutivo_id` | 2.403 |
| Toctoc con `gestion.ejecutivo_asignado` | 0 |
| **Universo repartible (DUEÑO_SEGURO + INCIERTO)** | **1.598** |

## 5. Índices actuales (8)

| Nombre | Key | Unique |
|--------|-----|--------|
| `_id_` | `{_id: 1}` | No |
| `origen_1_listing_id_1` | `{origen: 1, listing_id: 1}` | **Sí** |
| `idx_yapo_comuna_score` | `{details.comuna_norm: 1, score_captacion: -1}` | No |
| `idx_yapo_estado_comuna_score` | `{gestion.estado: 1, details.comuna_norm: 1, score_captacion: -1}` | No |
| `idx_yapo_gestion_ejecutivo_score` | `{gestion.estado: 1, gestion.ejecutivo_asignado: 1, score_captacion: -1}` | No |
| `idx_yapo_comuna_fecha_captura` | `{details.comuna_norm: 1, fecha_captura: -1}` | No |
| `idx_yapo_estado_comuna_fecha_captura` | `{gestion.estado: 1, details.comuna_norm: 1, fecha_captura: -1}` | No |
| `idx_yapo_gestion_ejecutivo_fecha_captura` | `{gestion.estado: 1, gestion.ejecutivo_asignado: 1, fecha_captura: -1}` | No |

## 6. Usuarios

| Rol | Cantidad |
|-----|----------|
| supervisor | 2 |
| agente | 8 |
| N/A (admin) | 1 |
| **Total** | **11** |

### Agentes activos con comunas de interés (5)

| Nombre | Comunas |
|--------|---------|
| Susana Ensignia | Ñuñoa, Providencia, La Reina, Las Condes, Vitacura, Macul, Santiago Centro |
| Mariela Arriagada | Santiago Centro, Providencia, Ñuñoa, Macul, San Miguel |
| Erika Garrido | La Florida, Ñuñoa, puente alto, peñalolén, penalolen, macul, providencia, las condes, la reina |
| Raquel Cheneaux | Las Condes, Ñuñoa, La Reina, Vitacura, Providencia, La Florida, Peñalolén, Macul |
| Paula Morales | Talca |

### Agentes activos sin comunas de interés (2)

- Pablo Galleguillos
- Rocío Aliaga

### Inactivos (1)

- María Paz Galleguillos

## 7. Referencias a `yapo_propiedades` en código

| Archivo | Ocurrencias | Tipo |
|---------|-------------|------|
| `api_captacion.py` | ~22 | find, update, create_index, distinct, count |
| `webhook.py` | ~10 | count, distinct, find_one, create_index, drop_index |
| `chatbot/metrics.py` | 3 | find_one, update_one |
| `chatbot/captacion_report.py` | 3 | distinct, find |
| `generar_reporte_captacion.py` | 1 | find (query) |
| `scraper_yapo/scraping_yapo_proxys.py` | 1 | collection name fallback |
| `improved_scraper.py` | 1 | deprecated |
| `.env` | 1 | MONGO_COLLECTION |
| `AGENTS.md` | 1 | doc reference |

### Scraping scripts con `db["yapo_propiedades"]` hardcoded

| Archivo | Operación |
|---------|-----------|
| `scraping/scraping_yapo_proxys_viejo.py` | update_one, queue |
| `scraping/targeted_rescrape_19.py` | update_one |
| `scraping/reprocess_parser_batch.py` | bulk_write |
| `scraping/recover_invalid_html.py` | update_one |
| `scraping/reclassify_batch_v4.py` | update_one |
| `scraping/backup_pre_clean.py` | insert_many |

## 8. Archivos modificables por config (MongoStore)

- `scraper/mongo_store.py` → usa `config.mongo_collection`
- `scraper/scraping_yapo_proxys_yapo.py` → usa `MongoStore`
- `scraper/extract_yapo_contact_phone.py` → usa `cfg.mongo_collection`
- `scraper/reprocess_reclassify.py` → usa `cfg.mongo_collection`
- `scraper/analyze_codigo_ref_local.py` → usa `cfg.mongo_collection`
- `scraper/run_full_pipeline.py` → usa `cfg.mongo_collection`

## 9. Universo a repartir (primera distribución)

- DUEÑO_SEGURO: 91
- INCIERTO: 1.507
- **Total potencial: 1.598**
- Ya asignados: 0
- Sin comuna_slug: por verificar
- Sin agente compatible: por verificar en dry-run

## 10. Riesgos

| Riesgo | Mitigación |
|--------|-----------|
| Encoding ñ en consola PowerShell | Usar scripts Python |
| Scrapers activos durante rename | Detener scrapers |
| Doble asignación | Filtro atómico en update |
| Yapo históricos modificados | $set-only, filtro origen |
| Templates no existen | Crear desde cero |
