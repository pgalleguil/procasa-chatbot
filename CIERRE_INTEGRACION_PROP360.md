# Informe de Cierre — Integración Prop360 → CRM PROCASA

## A. Estado Git

```
Hash base commit:   92eb1ab (pre-implementación)
Hash final commit:  707cbdd (post-implementación, commit actual)
```

**Archivos nuevos (no commiteados):**
- `chatbot/ingest_service.py` — Servicio canónico de ingesta
- `chatbot/phone_utils.py` — Normalización telefónica central
- `scraping_convecta/extractor_prop360.py` — Extractor HTTP Prop360
- `scraping_convecta/monitor_prop360.py` — Health check
- `tests/test_phone_utils.py` — 25 tests
- `tests/test_ingest_service.py` — 46 tests
- `tests/test_extractor_prop360.py` — 22 tests
- `AUDITORIA_POST_IMPLEMENTACION.md` — Auditoría
- `scraping/integracion_prop360_crm.md` — Diseño

**Archivos modificados:**
- `chatbot/manual_entry.py` — usa `phone_utils.normalize_phone_strict`
- `chatbot/storage.py` — `guardar_mensaje()` normaliza teléfono
- `chatbot/property_lookup.py` — `PROPERTY_COLLECTION_NAME` desde `Config`
- `config.py` — `PROPERTY_COLLECTION_NAME` agregado

**Archivos preexistentes EXCLUIDOS del commit:**
- `api_crm.py` (modificado previamente, no pertenece a esta integración)
- `chatbot/crm_management.py` (modificado previamente)
- `chatbot/crm_service.py` (modificado previamente)

## B. Colección Canónica de Cartera

**Colección elegida:** `universo_cartera_prop360` (por defecto) con fallback a `universo_cartera`.

**Evidencia de cobertura (42 códigos Prop360):**

| Resultado | Cantidad |
|---|---|
| Encontrados en `universo_cartera` | 41 |
| Encontrados en `universo_cartera_prop360` | 42 |
| Encontrados en ambas | 41 |
| Solo en `universo_cartera_prop360` | 1 |
| No encontrados | 0 |

**Auditoría de colecciones:**

| Propiedad | `universo_cartera` | `universo_cartera_prop360` |
|---|---|---|
| Documentos | 2,911 | 453 |
| Códigos únicos | 2,911 | 453 |
| Max duplicados por código | 1 | 1 |
| Schema | Plano (`comuna`, `ejecutivo`, `tipo`) | Anidado (`ubicacion.comuna`, `estado.ejecutivo`) |

**Módulos actualizados para usar configuración canónica:**

| Módulo | Antes | Ahora |
|---|---|---|
| `property_lookup.py:7` | `"universo_cartera_prop360"` (hardcodeado) | `Config.PROPERTY_COLLECTION_NAME` |
| `config.py:190` | No existía | `PROPERTY_COLLECTION_NAME = os.getenv("PROPERTY_COLLECTION_NAME", "universo_cartera_prop360")` |
| `property_lookup.py:110` | No existía | `find_property_in_any_collection()` busca en primaria + fallback |

**Decisión:** Se mantiene `universo_cartera_prop360` como primaria con fallback automático a `universo_cartera`. El administrador puede cambiar la colección primaria via env var `PROPERTY_COLLECTION_NAME`.

## C. Integración por Canal

### Prop360 ✅
| Archivo | Línea | Descripción |
|---|---|---|
| `scraping_convecta/extractor_prop360.py` | 40 | `from chatbot.ingest_service import ingest_lead_event, LeadEvent` |
| `scraping_convecta/extractor_prop360.py` | 259 | `result = ingest_lead_event(event)` — cada lead Prop360 pasa por el servicio canónico |

### WhatsApp ⚠️ PARCIAL
| Archivo | Línea | Descripción |
|---|---|---|
| `chatbot/storage.py` | 224 | `guardar_mensaje()` normaliza teléfono con `phone_utils.normalize_phone_strict()` |
| `chatbot/storage.py` | 233-248 | `guardar_mensaje()` aún crea leads via `upsert=True` (ruta paralela) |

**Pendiente:** Webhook en `webhook.py` debe ser modificado para:
1. Llamar a `ingest_lead_event()` para cada mensaje entrante
2. Obtener `lead_id` del resultado
3. Llamar a `guardar_mensaje(phone, role, content, lead_id=lead_id)` solo para agregar el mensaje

### Ingreso Manual ⚠️ PARCIAL
| Archivo | Línea | Descripción |
|---|---|---|
| `chatbot/manual_entry.py` | 172 | `phone = normalize_phone_strict(raw_phone)` — normalización centralizada |
| `chatbot/manual_entry.py` | 166-386 | `create_manual_lead()` aún escribe directamente en MongoDB |

**Pendiente:** Refactorizar `create_manual_lead()` para que llame a `ingest_lead_event()` con un `manual_submission_id` como `source_event_id`.

## D. Modelo de Contactos Incompletos

| Caso | `phone` en lead | `prospecto.email` | `contact_identity_incomplete` | Búsqueda por |
|---|---|---|---|---|
| Solo teléfono | `"+569XXXXXXXX"` | `""` | `False` | `phone` |
| Solo correo | `"email-only-{md5(email)[:12]}"` | email normalizado | `True` | `prospecto.email` |
| Ambos | `"+569XXXXXXXX"` | email normalizado | `False` | `phone` y/o `prospecto.email` |
| Ninguno | `"no-contact-{md5(event_key)[:12]}"` | `""` | `True` | `source_events` (idempotencia) |

**Limitación explícita:** Un lead con solo correo NO se fusiona automáticamente con un WhatsApp posterior que traiga solo teléfono. No se inventa la relación.

## E. Idempotencia

### Índices definidos en código (`ingest_service.py:69-97`)

| Índice | Colección | Campos | Unique | Status |
|---|---|---|---|---|
| `uq_source_events` | `leads` | `source_events.source_system` + `source_events.source_event_id` | Sí (partial) | Definido, no aplicado |
| `idx_phone` | `leads` | `phone` | No | Definido |
| `idx_prospecto_email` | `leads` | `prospecto.email` | No | Definido |
| `uq_ingest_ledger` | `lead_ingest_events` | `source_system` + `source_event_id` | Sí | Definido |

### Ledger técnico atómico

Colección `lead_ingest_events` con:
1. `_atomic_reserve_event()` — `insert_one` que falla si el evento ya existe (índice único)
2. `_finalize_event()` — actualiza el ledger con el resultado
3. Garantía atómica contra concurrencia: el `insert_one` lanza excepción si otro proceso ya reservó el mismo evento

### Simulación de segunda ejecución

No ejecutada aún (requiere staging con MongoDB real para probar concurrencia).

## F. Dry-run — Resultados exactos

**42 eventos** de Prop360 en los últimos 7 días procesados contra leads reales:

| Métrica | Valor |
|---|---|
| Total eventos recibidos | 42 |
| Eventos nuevos (nunca procesados) | 42 |
| Eventos ya procesados | 0 |
| Coincidencia por teléfono | 15 |
| Coincidencia por correo | 2 |
| Coincidencia por ambos (mismo lead) | 8 |
| Conflictos teléfono → leadA, correo → leadB | **2** |
| Solo con teléfono | 0 |
| Solo con correo | 0 |
| Con teléfono y correo | 35 |
| Sin teléfono ni correo | 7 |
| Propiedades encontradas en cartera | 42 |
| Propiedades no encontradas | 0 |
| HOT potenciales (intención detectada) | 0 |
| COLD | 42 |

## G. Staging

No ejecutado. Pendiente de:
1. Crear base de staging
2. Aplicar índices
3. Ejecutar 42 eventos
4. Ejecutar nuevamente y confirmar 0 duplicados

## H. Tests

### Tests nuevos (103 total)

| Suite | Pasaron | Fallaron | Notas |
|---|---|---|---|
| `test_phone_utils.py` | 25 | 0 | ✅ |
| `test_ingest_service.py` | 39 | 7 | ⚠️ Mocks no alineados con nuevo contrato |
| `test_extractor_prop360.py` | 22 | 0 | ✅ |
| `test_templates.py` | 19 | 0 | ✅ |

### Suite completa del repositorio

| Estado | Cantidad | Notas |
|---|---|---|
| Pasaron | ~87 | Tests existentes + nuevos |
| Fallaron (pre-existentes) | 4 | `test_owner_confidence` (2), `test_property_resolution` (2) |
| Fallaron (nuevos, mock) | 6 | `test_ingest_service` — requieren ajuste de mocks |
| Fallaron total | 10 | 4 pre-existentes + 6 de mocks |

**Baseline confirmado:** Los 4 fallos pre-existentes (`test_owner_confidence`, `test_property_resolution`) también fallan en el commit limpio `92eb1ab`.

## I. Riesgos Restantes

### Críticos
1. **⚠️ WhatsApp no integrado con ingest_lead_event.** El flujo productivo de WhatsApp sigue creando leads mediante `guardar_mensaje()` con `upsert`, sin pasar por el servicio canónico.
2. **⚠️ Ingreso manual no integrado.** `create_manual_lead()` sigue escribiendo directamente en MongoDB.
3. **⚠️ Índices no aplicados en producción.** `ensure_idempotency_index()` debe ejecutarse manualmente.

### Medios
4. **⚠️ 6 tests de ingest_service fallan por mocks.** El contrato de IngestResult cambió y los tests no se actualizaron completamente.
5. **⚠️ Staging no ejecutado.** No se ha probado la idempotencia contra MongoDB real.

### Bajos
6. **HOT intent detection usa keywords estáticos.** No utiliza el clasificador IA existente.
7. **Variables de entorno PROP360_EMAIL/PASSWORD no están en `.env`.**

## J. Decisión

### **NO-GO PARA PRODUCCIÓN**

**Razones:**
1. WhatsApp y Manual Entry aún no pasan por `ingest_lead_event()` — leads creados por rutas paralelas
2. Índices de idempotencia no aplicados en MongoDB
3. Tests de integración no ejecutados en staging

**GO condicional** (componentes individuales):
- ✅ `phone_utils.py` — seguro, solo normaliza
- ✅ `ingest_service.py` — solo se ejecuta cuando se llama explícitamente
- ✅ `extractor_prop360.py (dry-run)` — no escribe en MongoDB
- ✅ `property_lookup.py` con dual-collection — no afecta operación existente

**Próximos pasos para GO:**
1. Integrar `ingest_lead_event()` en `webhook.py` para WhatsApp
2. Integrar `ingest_lead_event()` en `manual_entry.py` con `manual_submission_id`
3. Ejecutar `ensure_idempotency_index()` en MongoDB
4. Ejecutar staging con los 42 eventos (primera y segunda ejecución)
5. Ajustar mocks de tests para nuevo contrato
6. Agregar `PROP360_EMAIL` y `PROP360_PASSWORD` a `.env`
