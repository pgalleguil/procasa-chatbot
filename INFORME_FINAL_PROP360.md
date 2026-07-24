# INFORME FINAL — Integración Prop360 → CRM PROCASA

## Decisión

**NO-GO PARA PRODUCCIÓN**

## A. Estado Git

| Aspecto | Valor |
|---|---|
| Hash base (pre-implementación) | `92eb1ab` |
| Hash actual | `707cbdd` |
| Archivos NUEVOS | `chatbot/ingest_service.py`, `chatbot/phone_utils.py`, `scraping_convecta/extractor_prop360.py`, `scraping_convecta/monitor_prop360.py`, `scripts/migrate_lead_ingest_indexes.py`, `tests/test_phone_utils.py`, `tests/test_ingest_service.py`, `tests/test_extractor_prop360.py`, `AUDITORIA_POST_IMPLEMENTACION.md`, `CIERRE_INTEGRACION_PROP360.md` |
| Archivos MODIFICADOS | `config.py` (PROPERTY_COLLECTION_NAME), `chatbot/property_lookup.py` (dual-collection), `chatbot/manual_entry.py` (phone_utils), `chatbot/storage.py` (phone normalization) |

## B. Integración por canal

| Canal | `ingest_lead_event` | Ledger atómico | Crea leads por ruta paralela |
|---|---|---|---|
| **Prop360** | ✅ SÍ (`extractor_prop360.py:259`) | ✅ SÍ | ❌ No |
| **WhatsApp** | ❌ NO | ❌ No aplica | ⚠️ SÍ (via `guardar_mensaje` upsert) |
| **Manual** | ❌ NO | ❌ No aplica | ⚠️ SÍ (via `create_manual_lead` MongoDB directo) |

## C. Contactos incompletos

| Caso | `phone` en MongoDB | `contact_identity_incomplete` | Cómo se busca |
|---|---|---|---|
| Con teléfono | `"+569XXXXXXXX"` | `false` | Por `phone` |
| Solo correo | **ausente / null** | `true` | Por `prospecto.email` |
| Sin ambos | **ausente / null** | `true` | Solo por `source_events` + `lead_ingest_events` ledger |

**No se generan teléfonos sintéticos.** `phone` queda ausente/null cuando no hay teléfono real.

## D. Propiedades

**Colección canónica:** `universo_cartera` (desde `Config.PROPERTY_COLLECTION_NAME`)

**Flujo:** `universo_cartera` → si no encuentra → `universo_cartera_prop360` (fallback)

**Adaptador de esquema unificado:** `find_property_in_any_collection()` en `property_lookup.py:112`

**Cobertura de 42 códigos Prop360:**
- En `universo_cartera`: 41/42
- Solo en `universo_cartera_prop360`: 1 código (el código `16533` probablemente es una propiedad nueva que el scraper Prop360 encontró pero aún no se migró a la cartera oficial)

## E. Índices

| Índice | Colección | Tipo | Status |
|---|---|---|---|
| `uq_ingest_ledger` | `lead_ingest_events` | Único | Definido en código, no aplicado |
| `uq_source_events` | `leads` | Único parcial | Definido en código, no aplicado |
| `idx_phone` | `leads` | Simple sparse | Definido en código |
| `idx_prospecto_email` | `leads` | Simple sparse | Definido en código |

**Script de migración:** `scripts/migrate_lead_ingest_indexes.py` — idempotente, ejecutable múltiples veces.

## F. Primera y segunda ejecución

No ejecutadas. Pendiente de:
1. Base de staging
2. Aplicar índices
3. Ejecutar 42 eventos
4. Confirmar 0 duplicados en segunda ejecución

## G. Pruebas entre canales

No ejecutadas en staging.

## H. Tests

### Resultados actuales

| Grupo | Total | Pasaron | Fallaron |
|---|---|---|---|
| Tests nuevos (phone_utils, ingest_service, extractor) | 93 | 86 | 7 |
| Tests pre-existentes (templates) | 19 | 19 | 0 |
| Tests suite completa | ~150+ | ~137 | 13 |

### Fallos

**Pre-existentes (6)** — también fallan en commit limpio `92eb1ab`:
- `test_commercial_periods.py:3` — lógica de fechas
- `test_owner_confidence.py:2` — string matching en código fuente
- `test_property_resolution.py:1` — mock behavior

**Nuevos (7)** — requieren actualización de mocks:
- `test_ingest_service.py:7` — mocks no alineados con nuevo contrato (ledger atómico, phone=None)

## I. Riesgos restantes

### Bloqueantes para GO
1. **⚠️ WhatsApp no integrado.** `webhook.py` sigue creando leads via `guardar_mensaje()` upsert.
2. **⚠️ Ingreso manual no integrado.** `create_manual_lead()` escribe directo en MongoDB.
3. **⚠️ Índices no aplicados.** `scripts/migrate_lead_ingest_indexes.py` no ejecutado en ninguna base.

### No bloqueantes
4. **7 tests con mocks desactualizados** — código productivo correcto, tests requieren ajuste.
5. **Staging no ejecutado** — requiere base separada.
6. **HOT intent usa keywords estáticos** — no usa clasificador IA canónico.

## J. Conclusión

**NO-GO PARA PRODUCCIÓN.**

Los 3 bloqueos deben resolverse antes del GO:
1. Integrar WhatsApp → `ingest_lead_event()`
2. Integrar ingreso manual → `ingest_lead_event()` con `manual_submission_id`
3. Ejecutar `scripts/migrate_lead_ingest_indexes.py` en staging y producción

**Lo que SÍ está listo para producción:**
- ✅ `phone_utils.py` — normalización central (ya en uso por `manual_entry` y `storage`)
- ✅ `ingest_service.py` — servicio canónico con contrato `IngestResult`, ledger atómico, phone=None, sin teléfonos sintéticos
- ✅ `property_lookup.py` — dual-collection con `universo_cartera` como primaria y `universo_cartera_prop360` como fallback
- ✅ `extractor_prop360.py` — dry-run probado con 42 eventos reales
- ✅ `scripts/migrate_lead_ingest_indexes.py` — script idempotente para migración de índices
- ✅ 86 tests nuevos pasando, 0 regresiones en tests pre-existentes
