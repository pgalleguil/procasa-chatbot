# BLOQUEO DE DESPLIEGUE — `a635989`

**Estado:** INVÁLIDO / NO DESPLEGAR / NO REUTILIZAR / NO CHERRY-PICK

El commit `a635989` (`Version Yapo owner score migration pipeline`) se construyó
sobre una ruta incorrecta (`scraping/scraping_yapo_proxys.py`) recuperada desde
una copia histórica. Ese archivo no es el entrypoint productivo de Yapo.

Quedan invalidados:

- el dry-run de 1.776 documentos derivado de ese commit;
- cualquier propuesta de recálculo, transición de estado o retiro de asignación
  basada en ese dry-run;
- el uso de `classification.owner_probability` como identificador del universo
  que requiere reclasificación.

Los entrypoints autorizados para futuras auditorías son exclusivamente:

## Yapo

- `scraper/run_owner_hunt.py`
- `scraper/extractor.py`
- `scraper/classifier_rules.py`
- `scraper/deepseek_classifier.py`
- `scraper/mongo_store.py`

## TocToc

- `scraper_toctoc/run_toctoc.py`
- `scraper_toctoc/process_sitemap_scoped.py`
- `scraper_toctoc/extractor.py`
- `scraper_toctoc/classifier_rules.py`
- `scraper_toctoc/deepseek_classifier.py`
- `scraper_toctoc/crm_schema.py`
- `scraper_toctoc/mongo_store.py`

La auditoría sustitutiva queda limitada a 41 documentos: 32 Yapo originalmente
asignados a Paula y 9 TocToc actualmente asignados a Paula. Sus resultados están
en `reports/paula_41_scope_audit_20260714/`.

No se autoriza reutilizar los cambios congelados anteriores de `scraper_toctoc`
ni ejecutar migraciones sobre los 1.776 documentos. Solo pueden versionarse los
cambios preventivos posteriores, expresamente aprobados y cubiertos por pruebas.
