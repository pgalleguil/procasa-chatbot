# Pricing Intelligence V1

Esta capa es una base analítica **read-only** y prospectiva. No crea
colecciones, índices, snapshots persistidos, rutas web, correos ni modelos.

## Problema de negocio

Preparar datos confiables para anticipar demanda futura y apoyar decisiones de
pricing, evitando que el estado actual de una propiedad contamine semanas
históricas.

## Unidad observacional futura

La unidad futura será `property × period`. El material base de esta fase es
`property × day`, representado por `PropertyDailySnapshotV1`.

El snapshot diario permite construir después semanas, ventanas de 7/30 días,
rolling windows, lags, cambios de precio y backtesting temporal.

## Regla as-of

Todas las features temporales cumplen estrictamente:

```text
event_time < as_of
```

Los timestamps deben ser timezone-aware. La zona de negocio es
`America/Santiago` y los cálculos también se normalizan a UTC. Un evento
exactamente igual al cutoff queda fuera.

V1 solo permite el snapshot operacional de la fecha local actual. No se puede
usar `as_of` de una fecha anterior para etiquetar el snapshot actual como
histórico. El builder falla con `HistoricalSnapshotNotSupported`.

## Identidad

La prioridad es `lead.prospecto.codigo` contra `codigo` de
`universo_cartera_prop360`. Después se usan únicamente aliases verificados:

- `prospecto.codigo_mercadolibre` contra los códigos `MLC...` de la estructura
  real `publicaciones.portal_inmobiliario.publicaciones.*.code`.
- `prospecto.codigo_yapo` contra `publicaciones.yapo.publicaciones.*.code`.

La estructura interna del identificador no se modifica: se aplica únicamente
Unicode NFKC, trim y conversión segura de scalar string/int a string.
Los aliases tienen namespace (`mercadolibre:...`, `yapo:...`). No se usan
direcciones, teléfonos, emails, nombres, fuzzy matching ni LLM.

## Precio y publicación

Los precios actuales se leen desde los bloques verificados de venta/arriendo.
Los cambios se leen desde `historial_cambios` embebido y
`universo_cartera_prop360_historial`, siempre antes del cutoff. Los duplicados
se deduplican por timestamp, unidad, valor anterior y valor nuevo, con una
prioridad de fuente determinística.

Las publicaciones contienen únicamente estado actual verificable e
identificador de aviso. No se inventan impresiones, views ni CTR.

`external_listing_views = NOT_AVAILABLE_V1`.

La fecha de alta comercial no es confiable para toda la cartera, por lo que
`listed_at` y `days_published` permanecen en `null` en V1.

## Demanda y targets futuros

Los leads enlazados de forma `EXACT_CANONICAL` o `EXACT_ALIAS` se cuentan en
las ventanas `[as_of - 7 días, as_of)` y `[as_of - 30 días, as_of)`. Los leads
ambiguos, conflictivos o no enlazados no se convierten en demanda positiva.

Todavía no se calculan targets. Quedan documentados para una fase posterior:

- `leads_next_7d`;
- `leads_next_30d`;
- `has_lead_next_7d`.

## PII

Los modelos V1 no almacenan email, teléfono, RUT, nombre, mensajes ni
dirección completa. Solo se conservan comuna y región. El CLI usa proyecciones
Mongo sin campos personales y genera reportes sin PII.

## Persistencia futura

La colección propuesta es `pricing_intelligence_property_snapshots_v1`.
El índice candidato, todavía no ejecutado, es:

```text
(snapshot_date_local, property_code)
```

`SnapshotRepository.persist()` permanece bloqueado intencionalmente. La CLI
solo llama a `build` y exige `--dry-run`.

## Ejecución

```bash
python -m analytics.pricing_intelligence.snapshot_cli --dry-run --sample 25
python -m analytics.pricing_intelligence.snapshot_cli --dry-run --report-json report.json
```

La configuración Mongo debe estar disponible mediante las variables de entorno
existentes del proyecto. Esta V1 no copia `.env` al worktree ni escribe en
MongoDB.
