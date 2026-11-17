# Yapo — controles de calidad de anunciante (17-11-2026)

Base: `codex/yapo-scraper-context-20261102`; cambios aislados en `codex/yapo-quality-gates-20261117`.

## Qué se corrigió

1. **Atribución de identidad**: cuando el HTML incluye una tarjeta identificada por «Publicado por», solo esa tarjeta aporta nombre, logo, badge y perfil Yapo. No se debe leer la identidad de tarjetas recomendadas/similares. Si el enlace al perfil no existe en la tarjeta actual, `seller_profile_id` permanece vacío.
2. **Falsos dueños**: las frases «venta de particular» o «vende dueño» no bastan para `DUEÑO_PROBABLE` cuando hay negaciones de titularidad, representación familiar/herencia, propietario tercero o nombre de contacto distinto. Se mantienen como `INCIERTO` con contradicciones trazables.
3. **Reportes auditables**: el CSV separa estado, razón, evidencia, revisión, disponibilidad y bloqueo de asignación. El resumen distingue `owner_probable_count`, `manual_review_count` y `assignment_ready_count`. El campo histórico `commercial_candidate_count` comprende dueño probable **o incierto para revisión**, no un universo asignable.

## Comprobación técnica

Desde la raíz del repositorio, con las dependencias instaladas:

```powershell
python -m pytest scrapers/scraper_yapo/tests/test_yapo_quality_gates_20261117.py scrapers/scraper_yapo/tests/test_general_search_engine.py scrapers/scraper_yapo/tests/test_broker_identity_evidence.py scrapers/scraper_yapo/tests/test_owner_language_exhaustive.py -q
```

Para ejecutar búsqueda en Yapo de Maipú desde el equipo local, con FlashProxy y la conexión Mongo **de solo lectura** previamente configurados:

```powershell
python scrapers\scraper_yapo\ejecutar_maipu.py
```

El script guarda JSON, CSV y resumen en `%USERPROFILE%\Documents\PROCASA\YapoMaipu\general_search`. Este flujo **no** importa a MongoDB, **no** distribuye, **no** notifica y **no** hace llamadas a DeepSeek.

**Control de parada**: una ejecución incompleta (`pagination_complete=false`, filtros no verificados, sin deduplicación confiable o errores de detalle) no debe aceptarse como conjunto comercial completo; el CLI retorna código 2. Revisar `errors_or_blocks`, `page_log` y `extraction_issues` por anuncio.

## Validación pendiente antes de producción

- Correr el flujo real contra varias páginas, incluidas transiciones de paginación React, con credenciales FlashProxy operativas y datos de Yapo vigentes. Las pruebas con fixtures no validan cambios nuevos del portal.
- Auditar una muestra estratificada de avisos **DUEÑO_PROBABLE**, **CORREDOR_SEGURO/PROBABLE** e **INCIERTO** contrastando la tarjeta de publicador, perfil, descripción y estado del aviso; registrar las etiquetas humanas como confirmadas/no verificadas, y no presentar opiniones como verdad verificada.
- Obtener una matriz de confusión sobre casos manualmente validados, especialmente falsos propietarios. Los datos históricos del lote de 482 anuncios no equivalen a evidencia actual ni validación en vivo.
- Acordar y verificar el proceso de revisión humana antes de habilitar cualquier escritura/importación o asignación. Conservar el bloqueo de disponibilidad no confirmada.

No se modificaron los scrapers anteriores, `main`, la distribución ni la colección canónica de captaciones.
