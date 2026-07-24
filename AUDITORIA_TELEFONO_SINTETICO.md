# Auditoría: Teléfono Sintético y Rutas del CRM

## A. Generación histórica del teléfono sintético

### Ubicación exacta

| Archivo | Línea | Función | Código |
|---|---|---|---|
| `chatbot/manual_entry.py` | 223 | `create_manual_lead()` | `final_phone = phone or f"no-phone-{now.timestamp()}"` |

**Formato:** `"no-phone-{now.timestamp()}"` — ejemplo: `"no-phone-1721784321.123456"`

**Características:**
- **NO determinístico:** Cada llamado genera un timestamp diferente. Un reintento genera un valor distinto.
- **Sin colisión práctica:** El timestamp Unix tiene precisión de microsegundos.
- **Confundible con teléfono real:** No, el prefijo `no-phone-` es explícito.
- **Uso:** Se guarda como el campo `phone` del documento lead en MongoDB.
- **Correo:** Sí se guarda por separado en `prospecto.email` (línea 271).
- **Búsqueda posterior:** Se usa `{"phone": final_phone}` para localizar el lead (líneas 294-300).
- **Ficha CRM:** Se usa en URL `/crm/lead/{phone_clean}` donde `phone_clean = re.sub(r"\D", "", phone)` → queda como `nophonel23456789` (solo dígitos del timestamp).

### Ocurrencias adicionales de `+56900000000`

| Archivo | Línea | Uso |
|---|---|---|
| `api_captacion.py` | 1038 | Teléfono dummy en tareas de captación (comentario: "el teléfono es un dummy") |
| `chatbot/processing_service.py` | 436 | Fallback cuando no hay ejecutivo |

**Guards que detectan `+56900000000`:**
- `chatbot/daily_report.py:319`
- `chatbot/metrics.py:257`
- `chatbot/processing_service.py:282`
- `chatbot/sla_service.py:70`
- `webhook.py:2877, 2889, 2897, 3093`

## B. Rutas dependientes del teléfono

### Rutas URL que usan phone como parámetro

| Ruta | Método | Archivo:Línea | Phone Source |
|---|---|---|---|
| `/crm/lead/{phone}` | GET | `webhook.py:1211` | URL path param |
| `/api/crm/log_action` | POST | `webhook.py:1241` | JSON body |
| `/api/crm/management-result` | POST | `webhook.py:1277` | JSON body |
| `/api/crm/update` | POST | `webhook.py:1311` | JSON body |
| `/api/crm/admin/reassign` | POST | `webhook.py:1350` | JSON body |
| `/api/crm/admin/mark-duplicate` | POST | `webhook.py:1389` | JSON body |
| `/api/crm/admin/archive` | POST | `webhook.py:1408` | JSON body |
| `/api/crm/notes` | POST | `webhook.py:1427` | JSON body |
| `/api/crm/send_recommendation` | POST | `webhook.py:1479` | JSON body |
| `/api/leads/check-duplicate` | GET | `webhook.py:1124` | Query param |

### Funciones generadoras de URLs CRM

| Función | Archivo:Línea | Qué hace cuando phone está vacío |
|---|---|---|
| `build_crm_lead_url()` | `lead_router.py:25-47` | Extrae dígitos con `re.sub(r"\D", "", phone)`. Si `phone_clean` vacío, retorna `/crm?temperatura=HOT` |
| `_lead_url()` | `templates.py:65-72` | Misma lógica: si phone vacío, retorna `/crm?temperatura=HOT` |

### Templates que usan phone en URLs

| Template | Línea | Código |
|---|---|---|
| `crm_leads_list.html` | 1875 | `data-lead-url="/crm/lead/{{ lead.phone }}"` |
| `manual_lead_entry.html` | 695 | `href="/crm/lead/${result.phone}?codigo=${result.property_code}"` |
| `crm_lead_detail.html` | ~3677-5425 | Múltiples fetch a `/api/crm/*` con phone en JSON body |
| `chat_detail.html` | (webhook.py:1102) | `"phone": phone` en contexto de template |

## C. Consultas MongoDB con `{"phone": phone}`

**Total:** ~50 ocurrencias en 15+ archivos

| Archivo | # Queries | Propósito principal |
|---|---|---|
| `chatbot/storage.py` | 16 | Guardar mensajes (upsert), obtener/conversaciones, prospecto, propiedades vistas, estado |
| `chatbot/crm_service.py` | 1 | Actualizar intent/temperatura |
| `api_crm.py` | 5 | Obtener eventos, tareas, actualizar datos |
| `chatbot/core.py` | 3 | Procesar mensajes del chatbot |
| `chatbot/metrics.py` | 1 | Backfill de eventos |
| `chatbot/sla_service.py` | 2 | SLA warnings |
| `chatbot/alert_service.py` | 1 | Alertas |
| `chatbot/manual_entry.py` | 2 | Duplicate check + creación |
| `chatbot/document_message_guard.py` | 1 | Document guard |
| `chatbot/ingest_service.py` | 1 | Dedup por teléfono |
| `chatbot/notification_service.py` | 1 | Dedup de notificaciones |
| `api_leads_intelligence.py` | 1 | Obtener chat |
| `webhook.py` | 1 | Scheduled tasks |
| `recover_missing_leads.py` | 1 | Recuperación |
| `api_contracts.py`, `api_visitas.py`, `api_captacion.py` | 3 | Registro de actividades |

## D. Impacto de `phone = None`

### Funciones que se rompen:

1. **`guardar_mensaje()`** en `storage.py` — usa `{"phone": phone}` con `upsert=True`. Con `phone=None`, crearía leads con `phone=null` que no podrían encontrarse después.

2. **`CrmService.get_lead(phone)`** en `crm_service.py:28` — busca con regex sobre phone. Con None, fallaría.

3. **`/crm/lead/{phone}`** en `webhook.py:1211` — la ruta recibe phone como string obligatorio. Con None, la URL sería `/crm/lead/None`.

4. **Todas las 8 rutas `/api/crm/*`** — validan `if not phone: raise HTTPException(400, "Falta teléfono")`.

5. **`build_crm_lead_url()`** — con `phone=""`, retorna fallback a `/crm?temperatura=HOT`, no al lead específico.

6. **`crm_leads_list.html:1875`** — `data-lead-url="/crm/lead/{{ lead.phone }}"` generaría `/crm/lead/None`.

7. **`manual_lead_entry.html:695`** — href apuntaría a `/crm/lead/None`.

8. **Notificaciones WhatsApp** — los enlaces a leads usarían `/crm?...` sin identificar el lead.

9. **50 consultas MongoDB** que usan phone como filtro no encontrarían nada.

### Funciones que NO se rompen:

- Búsqueda por `prospecto.email` (ya implementada en `ingest_service.py`)
- Idempotencia por `source_events` (nueva funcionalidad)
- Pipeline y temperatura (dependen de `_id`, no de phone)
- Asignación (usa `ejecutivo_asignado`)
- Métricas de analytics (usan agregaciones por pipeline_stage, no por phone)

## E. Modelo recomendado

**MANTENER TELÉFONO SINTÉTICO CONTROLADO (Opción A modificada)**

### Justificación

El CRM está construido sobre la premisa de que `phone` es la clave primaria del lead:
- 15+ archivos dependen de `{"phone": phone}` para consultas
- Las rutas `/crm/lead/{phone}` y 8 endpoints `/api/crm/*` usan phone como identificador
- Los templates generan URLs con `lead.phone`
- `guardar_mensaje()` hace upsert por phone
- `build_crm_lead_url()` normaliza phone a dígitos para la URL

Eliminar el teléfono sintético requeriría refactorizar ~50 consultas, ~10 rutas, ~4 templates y ~2 generadores de URL. Ese cambio no puede hacerse sin una migración planificada.

### Reglas obligatorias (implementar ahora)

```
1. phone_is_synthetic = True         ← nuevo flag en el documento lead
2. contact_phone = None              ← teléfono real (None si no existe)
3. contact_phone_normalized = None   ← teléfono normalizado
```

Estas reglas ya se aplican en `ingest_service.py`:
- `contact_identity_incomplete = True` cuando no hay teléfono
- `phone = None` para contactos sin teléfono (en el nuevo código)

Pero para `create_manual_lead()`, se necesita:
- Marcar `phone_is_synthetic = True` cuando se genera `no-phone-{timestamp}`
- No usar el teléfono sintético para búsquedas de identidad (usar correo)
- No mostrarlo como teléfono del cliente en la interfaz

### Plan de transición

1. **Corto plazo (ahora):** Agregar `phone_is_synthetic` y `contact_phone_raw` a los leads creados sin teléfono. No cambiar rutas existentes.
2. **Mediano plazo:** Agregar `lead.conversation_id` como parámetro alternativo en rutas CRM (ej: `/crm/lead/{conversation_id}`).
3. **Largo plazo:** Migrar búsquedas a `_id` o `conversation_id`.

## F. Decisión sobre Prop360

### Almacenamiento de contactos sin teléfono

| Caso | Campo `phone` | `prospecto.email` | `contact_identity_incomplete` | `phone_is_synthetic` |
|---|---|---|---|---|
| Solo teléfono | `+569XXXXXXXX` | `""` | `false` | `false` |
| Solo correo | `no-phone-{timestamp}` | `correo@mail.com` | `true` | `true` |
| Teléfono y correo | `+569XXXXXXXX` | `correo@mail.com` | `false` | `false` |
| Sin ambos | `no-phone-{timestamp}` | `""` | `true` | `true` |

### Reglas de identidad

1. Si `phone` existe y NO es sintético → buscar y deduplicar por `phone`
2. Si `phone` es sintético (`phone_is_synthetic == true`) → NO usar phone para identidad
3. Si existe `prospecto.email` → buscar y deduplicar por email
4. Si no hay ni phone real ni email → solo idempotencia por `lead_ingest_events`
5. No fusionar por nombre
6. No enviar WhatsApp a teléfonos sintéticos
7. No mostrar teléfono sintético como contacto del cliente

### Impacto en `create_manual_lead()` actual

La función actual en `manual_entry.py:223` genera `no-phone-{timestamp}`. Este comportamiento:
- **NO se modifica ahora** (para no romper la pantalla de ingreso manual)
- **Se complementa** agregando `phone_is_synthetic = True` al documento
- **Se documenta** como comportamiento conocido controlado

El nuevo extractor Prop360 (`ingest_service.py`) ya usa el modelo sin teléfonos sintéticos:
- `final_phone = None` para leads sin teléfono (línea 449)
- `contact_identity_incomplete = True` (línea 451)
- Búsqueda por `prospecto.email` o `source_events`

Pero `create_manual_lead()` seguirá usando el teléfono sintético hasta la migración.

## G. Conclusión

El CRM actual depende profundamente de `phone` como clave técnica. Cambiar a `phone=None` rompería el listado, detalle, API y notificaciones. La estrategia correcta es:

1. **Prop360 (extractor nuevo):** Usar `phone=None` + `contact_identity_incomplete=True` + búsqueda por email/source_events (ya implementado en `ingest_service.py`)
2. **create_manual_lead() actual:** Mantener teléfono sintético pero agregar `phone_is_synthetic=True` (no rompe nada existente)
3. **guardar_mensaje() actual:** Mantener upsert por phone para no romper WhatsApp
4. **Plan de migración:** Agregar rutas por `conversation_id` como alternativa, migrar progresivamente
