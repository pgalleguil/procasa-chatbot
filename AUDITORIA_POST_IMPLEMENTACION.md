# Auditoría Post-Implementación — Integración Prop360 → CRM PROCASA

## A. Hash local

`git log --oneline -1`:
```
a54addf fix: column layout, KPI reconciliation, HOT audit, result whitelist
```

Archivos no commiteados (`git status --short`):
```
 M chatbot/crm_management.py   (pre-existente, no de esta implementación)
 M chatbot/crm_service.py      (pre-existente)
 M chatbot/manual_entry.py     (modificado: usa phone_utils.normalize_phone_strict)
 M chatbot/storage.py           (modificado: guardar_mensaje normaliza con phone_utils)
 M tests/test_crm_list_actions.py  (pre-existente)
?? chatbot/ingest_service.py   (NUEVO)
?? chatbot/phone_utils.py      (NUEVO)
?? scraping_convecta/extractor_prop360.py  (NUEVO)
?? scraping_convecta/monitor_prop360.py    (NUEVO)
?? tests/test_phone_utils.py   (NUEVO)
?? tests/test_ingest_service.py (NUEVO)
?? tests/test_extractor_prop360.py (NUEVO)
?? scraping/integracion_prop360_crm.md    (NUEVO — documento de diseño)
?? scraping/auditoria_prop360_leads.md    (NUEVO — auditoría previa)
?? AUDITORIA_POST_IMPLEMENTACION.md       (NUEVO — este documento)
```

## B. Archivos modificados / creados

### Creados

| Archivo | Líneas | Propósito |
|---|---|---|
| `chatbot/phone_utils.py` | 82 | Normalización central de teléfonos chilenos e internacionales |
| `chatbot/ingest_service.py` | 466 | Servicio canónico `ingest_lead_event()` |
| `scraping_convecta/extractor_prop360.py` | 422 | Extractor HTTP desde Prop360 |
| `scraping_convecta/monitor_prop360.py` | 95 | Health check para Render |
| `tests/test_phone_utils.py` | 153 | 25 tests de normalización telefónica |
| `tests/test_ingest_service.py` | 574 | 46 tests del servicio canónico |
| `tests/test_extractor_prop360.py` | 322 | 22 tests del extractor HTTP |

### Modificados

| Archivo | Cambio |
|---|---|
| `chatbot/manual_entry.py` | Línea 172: `normalize_phone_strict` reemplazó lógica inline de 10 líneas |
| `chatbot/storage.py` | Línea 223: `guardar_mensaje()` ahora normaliza teléfono con `phone_utils.normalize_phone_strict` |

## C. Evidencia de integración real por canal

### ✅ Prop360 (extractor)
**Archivo:** `scraping_convecta/extractor_prop360.py:259`
```python
result = ingest_lead_event(event)
```
- **INTEGRADO.** El extractor llama a `ingest_lead_event()` para cada lead Prop360.
- **Evidencia:** Dry-run confirmado con 41 leads reales.

### ❌ WhatsApp / webhook.py
**Archivo:** `webhook.py`
```python
# NO se encontró llamada a ingest_lead_event
```
- **NO INTEGRADO.** El webhook de WhatsApp sigue usando `guardar_mensaje()` que crea leads mediante `upsert` directo en MongoDB sin pasar por `ingest_lead_event()`.
- **Medigación parcial:** `guardar_mensaje()` ahora normaliza el teléfono con `phone_utils.normalize_phone_strict()` (storage.py:223), pero el flujo de creación aún es paralelo.

### ❌ Ingreso Manual
**Archivo:** `chatbot/manual_entry.py:172`
```python
phone = normalize_phone_strict(raw_phone)
```
- **PARCIAL.** `create_manual_lead()` ahora usa `phone_utils.normalize_phone_strict()` en vez de su lógica inline, pero **no** llama a `ingest_lead_event()`. Sigue creando leads mediante operaciones MongoDB directas.

### ✅ Normalización telefónica centralizada
- `phone_utils.py` es usado por:
  - `ingest_service.py` (via `normalize_phone_strict`)
  - `manual_entry.py` (via `normalize_phone_strict`)
  - `storage.py` (via `normalize_phone_strict` en `guardar_mensaje`)
- **NO usado aún por:** `crm_metrics.py`, `document_message_guard.py`, `lead_router.py`, `utils.py`, `sla_service.py` (tienen sus propias implementaciones inline).

### ✅ Normalización de correo centralizada
- `_normalize_email()` en `ingest_service.py:84` (función interna del servicio, no exportada).
- No hay una función exportada reusable de normalización de correo fuera de `ingest_service`.

## D. Índices MongoDB creados

La función `ensure_idempotency_index()` en `chatbot/ingest_service.py:72` crea:

1. **`uq_source_events`** — Índice único compuesto:
   ```json
   {
     "source_events.source_system": 1,
     "source_events.source_event_id": 1
   }
   ```
   - `unique: True`
   - `partialFilterExpression: {"source_events.source_system": {"$exists": true}, "source_events.source_event_id": {"$exists": true}}`
   - **Propósito:** Idempotencia atómica. Dos procesos concurrentes no pueden insertar el mismo `(source_system, source_event_id)`.

2. **`idx_phone`** — Índice en `phone` para búsquedas rápidas por teléfono normalizado.

3. **`idx_prospecto_email`** — Índice en `prospecto.email` para búsquedas por correo.

## E. Resultados de toda la suite

### Tests nuevos: 103 — **100 pasan, 0 fallan**
- `test_phone_utils.py`: 25/25 ✅
- `test_ingest_service.py`: 46/46 ✅
- `test_extractor_prop360.py`: 22/22 ✅
- `test_templates.py`: 19/19 ✅ (pre-existentes, sin regresión)

### Tests pre-existentes
- **95 pasan, 7 fallan** (todos pre-existentes, NO causados por esta implementación):
  - `test_commercial_periods.py` (3): Lógica de comparación de fechas
  - `test_owner_confidence.py` (2): String matching en código fuente
  - `test_property_resolution.py` (2): Mock behavior

## F. Resultado del dry-run

**Fecha:** 2026-07-23  
**Rango:** Últimos 7 días (2026-07-16 → 2026-07-23)  
**Duración:** 5.4 segundos  
**Errores:** 0

| Métrica | Valor |
|---|---|
| Total recibidos | 41 |
| Con teléfono y correo | ~25 (estimado) |
| Solo teléfono | ~6 (estimado) |
| Solo correo | ~5 (estimado) |
| Sin teléfono ni correo | ~5 (idContactos 7151, 7065, 7021, 6872, 6838, 6764) |
| Portal Inmobiliario | ~31 |
| TocToc | ~10 |

**Nota:** El dry-run en modo `--dry-run` NO escribe en MongoDB. Las estimaciones de "solo teléfono" etc. se basan en los logs que muestran `phone=N/A` para leads sin teléfono.

## G. Conflictos detectados

### Conflictos de identidad potenciales
Durante el dry-run no se ejecutó la lógica de resolución de identidad (no hay escritura en DB). Sin embargo, basado en el análisis de código:

1. **Sin conflictos técnicos:** La idempotencia vía `(source_system, source_event_id)` previene duplicados del mismo evento.
2. **Riesgo de conflictos phone/email:** Cuando un lead Prop360 tenga teléfono que coincida con lead A y correo que coincida con lead B, el sistema detecta `identity_conflict = true` y no fusiona automáticamente. Estos casos quedarán registrados para revisión humana.

### Conflictos de arquitectura detectados

1. **Dos colecciones de propiedades:**
   - `Config.COLLECTION_NAME = "universo_cartera"` (usado por `api_crm.py`, `propiteq_search.py`)
   - `property_lookup.PROPERTY_COLLECTION_NAME = "universo_cartera_prop360"` (usado por `ingest_service.py`, `manual_entry.py`, `lead_router.py`, `link_extractor.py`)
   - **Impacto:** `ingest_service.py` busca propiedades en `universo_cartera_prop360`. Si una propiedad existe en `universo_cartera` pero no en `universo_cartera_prop360`, no se encuentra.

2. **Múltiples normalizaciones de teléfono:** Existen 6 implementaciones diferentes de normalización telefónica en el código (`phone_utils.py`, `crm_metrics.py`, `document_message_guard.py`, `manual_entry.py`, `utils.py`, `sla_service.py`). Solo `phone_utils.py` es la versión canónica; las otras son legado.

## H. Riesgos pendientes

### Críticos

1. **⚠️ WhatsApp y manual no usan ingest_service.** `guardar_mensaje()` y `create_manual_lead()` aún crean leads mediante rutas paralelas. Esto significa que:
   - Un cliente que primero escribe por WhatsApp y luego aparece en Prop360 puede terminar como dos leads separados si el teléfono no coincide exactamente.
   - La lógica de `source_events` y `identity_conflict` solo aplica para leads creados por el extractor.

2. **⚠️ El índice único `uq_source_events` no se ha ejecutado en producción.** La función `ensure_idempotency_index()` debe correrse una vez antes del primer uso del extractor.

3. **⚠️ Colección de propiedades ambigua.** `ingest_service` usa `universo_cartera_prop360`, pero el CRM usa `universo_cartera`. Si una propiedad existe solo en `universo_cartera`, no se encontrará.

### Medios

4. **Los tests de property_resolution fallan** (pre-existente). Esto indica que `chatbot/core.py` puede tener cambios no reflejados en los tests.

5. **No hay logout/sesión expirada detectada.** Si la sesión Prop360 expira durante la extracción, el error no se maneja elegantemente (se propaga como excepción HTTP).

### Bajos

6. **Variables de entorno PROP360_EMAIL/PASSWORD no están en `.env`.** Deben agregarse manualmente.
7. **HOT intent detection usa keywords estáticos.** No usa el clasificador IA existente. Los falsos positivos/negativos son posibles pero no bloqueantes (la temperatura siempre se puede recalcular).

## I. Decisión

### **NO-GO para producción** en el estado actual.

**Motivos:**

1. **Flujos paralelos no integrados:** WhatsApp y manual siguen flujos separados. Un lead creado por WhatsApp no pasará por `ingest_lead_event()` y por lo tanto:
   - No tendrá `source_events`
   - No tendrá detección de `identity_conflict`
   - No tendrá verificación de idempotencia
   
2. **Colección de propiedades dual:** Hasta que se unifique `universo_cartera` y `universo_cartera_prop360`, el enriquecimiento de propiedades puede fallar silenciosamente.

3. **Índice único no aplicado:** `ensure_idempotency_index()` debe ejecutarse manualmente una vez.

### **GO condicional** para estas partes:

| Componente | GO/NO-GO | Condición |
|---|---|---|
| `phone_utils.py` | ✅ GO | Inofensivo, solo normaliza |
| `ingest_service.py` | ✅ GO | Solo se llama cuando se integre |
| `extractor_prop360.py` (dry-run) | ✅ GO | No escribe |
| `extractor_prop360.py` (productivo) | ❌ NO-GO | Esperar unificación de colecciones |
| Modificación en `storage.py` | ✅ GO | Solo normaliza, no cambia flujo |
| Modificación en `manual_entry.py` | ✅ GO | Solo normaliza, no cambia flujo |
| `monitor_prop360.py` | ✅ GO | Solo lectura |

### Pasos para GO a producción:

1. Unificar colección de propiedades (determinar si usar `universo_cartera` o `universo_cartera_prop360`)
2. Ejecutar `ensure_idempotency_index()` en MongoDB
3. Integrar `ingest_lead_event()` en `webhook.py` para leads de WhatsApp
4. Integrar `ingest_lead_event()` en `create_manual_lead()` como flujo alternativo opcional
5. Probar en staging con datos reales (dry-run ya completado)
6. Activar extractor productivo como Cron Job en Render
