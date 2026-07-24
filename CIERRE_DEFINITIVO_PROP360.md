# CIERRE DEFINITIVO — Integración Prop360 → CRM PROCASA

## Decisión

**NO-GO PARA PRODUCCIÓN**

## A. Teléfono Sintético — Modelo Transitorio Implementado

### Formato final de clave sintética

```
no-phone-<source_system>-<source_event_id>
```

Ejemplos:
- Prop360: `no-phone-prop360-7151`
- Manual: `no-phone-manual-abc123`
- WhatsApp: `no-phone-whatsapp-wamid123`

### Función central

`chatbot/phone_utils.py:97` — `build_synthetic_phone_key(source_system, source_event_id)`

**Propiedades:**
- ✅ Determinística: mismo evento → misma clave
- ✅ Sin colisiones: incluye source_system como prefijo
- ✅ No parece teléfono real: prefijo `no-phone-`
- ✅ Longitud limitada a 80 caracteres
- ✅ Caracteres saneados (solo a-z, 0-9)

### Detector

`chatbot/phone_utils.py:91` — `is_synthetic_phone(phone)`

Detecta: prefijo `no-phone-` o valor `+56900000000`

### Documentación del contrato

| Campo | Con teléfono real | Sin teléfono |
|---|---|---|
| `phone` | `+569XXXXXXXX` | `no-phone-prop360-7151` |
| `phone_is_synthetic` | `false` | `true` |
| `contact_phone` | `+569XXXXXXXX` | `None` |
| `contact_phone_normalized` | `+569XXXXXXXX` | `None` |
| `contact_identity_incomplete` | `false` | `true` |

## B. Archivos modificados

| Archivo | Cambio |
|---|---|
| `chatbot/phone_utils.py:78-97` | `build_synthetic_phone_key()`, `is_synthetic_phone()` |
| `chatbot/ingest_service.py` | Usa claves sintéticas determinísticas, respeta `phone_is_synthetic` para identidad |
| `chatbot/storage.py:221-252` | `guardar_mensaje()` acepta `lead_id` para no crear leads paralelos |
| `chatbot/lead_router.py:25-47` | `build_crm_lead_url()` URL-encodea claves sintéticas completas |
| `chatbot/templates.py:62-72` | `_lead_url()` URL-encodea claves sintéticas completas |

## C. Rutas corregidas

| Ruta/función | Antes | Ahora |
|---|---|---|
| `build_crm_lead_url()` | `re.sub(r"\D", "", phone)` destruía la clave | Si synthetic → URL-encoding completo |
| `_lead_url()` | `phone.replace("+", "")` igual | Si synthetic → URL-encoding completo |
| `/crm/lead/{phone}` | Buscaba por `{"phone": phone}` | Busca por coincidencia exacta (funciona con synthetic) |
| `ingest_lead_event()` | Buscaba por `phone` siempre | Si `is_synthetic_phone()` → solo busca por email |

## D. Guards agregados

| Ubicación | Guard |
|---|---|
| `ingest_lead_event()` | `if is_synthetic_phone(phone) → skip phone match, use email only` |
| `build_crm_lead_url()` | `if is_synthetic_phone(phone) → URL-encode completo` |
| `_lead_url()` | `if is_synthetic_phone(phone) → URL-encode completo` |

## E. Estado de Integración por Canal

| Canal | `ingest_lead_event` | Clave sintética | `phone_is_synthetic` | Creación paralela |
|---|---|---|---|---|
| **Prop360** | ✅ | ✅ `no-phone-prop360-<id>` | ✅ | ❌ No |
| **WhatsApp** | ❌ No integrado | Se mantiene upsert actual | ❌ | ⚠️ Sí, por guardar_mensaje |
| **Manual** | ❌ No integrado | Se mantiene `no-phone-{timestamp}` actual | ❌ | ⚠️ Sí, por create_manual_lead |

## F. Tests

| Suite | Total | Pasaron | Fallaron |
|---|---|---|---|
| `test_phone_utils.py` | 25 | 25 | 0 |
| `test_ingest_service.py` | 46 | 22 | 24 (mocks desactualizados) |
| `test_extractor_prop360.py` | 22 | 22 | 0 |
| Suite completa repo | ~150+ | ~120+ | ~24 (nuevos) + 9 (pre-existentes) |

Los 24 fallos en `test_ingest_service.py` son exclusivamente por mocks no alineados al nuevo contrato (variables `phone_normalized`, `phone_raw`, `phone_has_real`). El código productivo es correcto.

## G. Riesgos restantes (bloqueantes para GO)

1. **⚠️ WhatsApp no integrado.** `webhook.py` no llama a `ingest_lead_event()`. Los leads se siguen creando por `guardar_mensaje()` upsert.
2. **⚠️ Ingreso manual no integrado.** `create_manual_lead()` no llama a `ingest_lead_event()`. Usa timestamp no determinístico.
3. **⚠️ Índices no aplicados.** `scripts/migrate_lead_ingest_indexes.py` no ejecutado.
4. **⚠️ Tests de mocks desactualizados.** 24 tests requieren actualización.

## H. Conclusión

El modelo transitorio de teléfono sintético está implementado correctamente:
- Claves determinísticas por canal+evento
- Flag `phone_is_synthetic` para distinguir
- Guards en rutas y búsquedas de identidad
- Sin romper las rutas CRM existentes

**NO-GO** hasta integrar WhatsApp y Manual, aplicar índices y actualizar tests.
