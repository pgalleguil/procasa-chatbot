# Pricing Intelligence V1

Esta capa es una base analítica prospectiva. Construye snapshots sin efectos
operacionales y, únicamente con `--persist --confirm-production-write`, puede
escribir en las dos colecciones V1 nuevas descritas más abajo. No modifica
colecciones operacionales, rutas web, correos, scheduler ni modelos.

## Problema de negocio

Preparar datos confiables para anticipar demanda futura y apoyar decisiones de
pricing, evitando que el estado actual de una propiedad contamine semanas
históricas.

## Unidad observacional futura

La unidad futura será `property × period`. El material base de esta fase es
`property × day`, representado por `PropertyDailySnapshotV1`.

El snapshot diario permite construir después semanas, ventanas de 7/30 días,
rolling windows, lags, cambios de precio y backtesting temporal.

## Regla as-of y semántica diaria

Todas las features temporales cumplen estrictamente:

```text
event_time < as_of
```

Los timestamps deben ser timezone-aware. La zona de negocio es
`America/Santiago` y los cálculos también se normalizan a UTC. Un evento
exactamente igual al cutoff queda fuera.

El snapshot observa el estado disponible en el cutoff explícito de la
ejecución. No representa "todo el día" ni se reconstruye históricamente: V1
solo permite el snapshot operacional de la fecha local actual. El builder
falla con `HistoricalSnapshotNotSupported` si se intenta etiquetar una fecha
histórica. Todos los documentos de un run comparten exactamente un mismo
`as_of_local` y `as_of_utc`, capturados una sola vez al inicio lógico del run.

La granularidad es como máximo un snapshot por `property_code` y
`snapshot_date_local`. El documento es inmutable después de insertarse. En
fases posteriores, los snapshots históricos podrán formar `property_week` y
features rolling sin usar información futura; los targets futuros no forman
parte del snapshot diario.

## Identidad

La prioridad es `lead.prospecto.codigo` contra `codigo` de
`universo_cartera_prop360`. Después se usan únicamente aliases verificados:

- `prospecto.codigo_mercadolibre` contra los códigos `MLC...` de la estructura
  real `publicaciones.portal_inmobiliario.publicaciones.*.code`.
- `prospecto.codigo_yapo` contra `publicaciones.yapo.publicaciones.*.code`.

Cuando `prospecto.origen` normaliza inequívocamente a `toctoc`,
`prospecto.codigo_propiedad` y `prospecto.propiedad_codigo` se aceptan solo
contra un alias TOCTOC único. Sin fuente TOCTOC no se resuelven.

La estructura interna del identificador no se modifica: se aplica únicamente
Unicode NFKC, trim y conversión segura de scalar string/int a string.
Los aliases tienen namespace (`mercadolibre:...`, `yapo:...`). No se usan
direcciones, teléfonos, emails, nombres, fuzzy matching ni LLM.

La normalización de etiquetas de portal conserva namespaces separados. Por
ejemplo, `TocToc`, `toc toc` y `TOCTOC` pueden identificar la etiqueta TOCTOC,
pero nunca convierten un ID TOCTOC en un ID MercadoLibre o Yapo.

Unmatched is a valid analytical outcome and must not be converted into a match
through heuristic inference.

Los códigos canonical ausentes de la maestra actual pueden compararse de forma
diagnóstica con `universo_cartera`, `universo_obelix` e
`ingresos_supervisados`, pero nunca se resuelven operativamente contra una
colección legacy. Un código encontrado solo allí permanece `UNMATCHED`.

La V2 agrega únicamente aliases contextuales TOCTOC para
`codigo_propiedad`/`propiedad_codigo` cuando el origen declarado es TOCTOC y
existe exactamente un candidato. La cobertura ganada se reporta por separado
de la calidad; los casos `UNMATCHED`, `AMBIGUOUS` y `CONFLICT` permanecen
explícitos.

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

## Persistencia V1

Las únicas colecciones autorizadas son:

- `pricing_intelligence_property_snapshots_v1`: documentos diarios
  inmutables, con `_id = v1:{snapshot_date_local}:{normalized_safe_property_code}`.
- `pricing_intelligence_snapshot_runs_v1`: ledger auditable de cada run, sin
  PII, con estado `RUNNING`, `COMPLETED`, `PARTIAL` o `FAILED`.

El `_id` es determinístico; no se usa UUID para snapshots. Los UUID se usan
solo para `run_id`. La escritura de snapshots es `insert_many` por lotes de
250, sin `upsert`, `replace_one` ni actualización de documentos existentes.
Si el `_id` ya existe, se valida fecha, código, schema y contenido; si es
igual se cuenta como `skipped_existing`, y si difiere se aborta por
`INCONSISTENT_SNAPSHOT_STATE`.

Los reruns del mismo día son idempotentes. Un run completo se informa como
`ALREADY_COMPLETE` sin crear otro ledger. Un run `RUNNING`/`PARTIAL` reutiliza
su cutoff original y llena únicamente snapshots faltantes. Snapshots
existentes sin un run coherente abortan. Un cambio superior a ±20% en el
conteo de propiedades de la maestra frente al último run completo anterior
aborta con `SOURCE_COUNT_ANOMALY`; el cambio de leads es solo advertencia
analítica.

Índices creados exclusivamente en estas colecciones:

```text
pricing_intelligence_property_snapshots_v1:
  snapshot_date_local
  (property_code, snapshot_date_local)
pricing_intelligence_snapshot_runs_v1:
  snapshot_date_local
  status
```

La protección PII valida claves recursivamente y aborta si encuentra email,
phone/teléfono, RUT, nombres de propietario, mensajes, dirección, user agent,
IP o variantes normalizadas. Nunca elimina silenciosamente una clave
prohibida.

## Ejecución

```bash
python -m analytics.pricing_intelligence.snapshot_cli --dry-run --sample 25
python -m analytics.pricing_intelligence.snapshot_cli --dry-run --report-json report.json
python -m analytics.pricing_intelligence.snapshot_cli --persist --confirm-production-write
```

`--dry-run` y `--persist` son mutuamente excluyentes. `--persist` no admite
`--sample` ni `--property-code` y exige la confirmación explícita no
interactiva. La configuración Mongo debe estar disponible mediante las
variables de entorno existentes del proyecto. Esta V1 no copia `.env` al
worktree ni escribe en colecciones operacionales.

## Owner Portal PROCASA SUCRE (prototipo interno)

Esta fase agrega una vista experimental de Customer-facing Analytics para
PROCASA SUCRE. No crea snapshots nuevos: lee la maestra existente, los
snapshots generales ya persistidos y las fuentes de mercado. `office_scope` es
una dimensión de lectura (`PROCASA_SUCRE`), no una colección ni una copia por
oficina.

### Regla de identidad de oficina

La regla canónica y reutilizable es:

```text
is_procasa_sucre_property(doc)
  == (doc["estado"]["oficina"] == "PROCASA SUCRE")
```

El campo y valor fueron auditados en `universo_cartera_prop360`. No se usan
alias para `INMOBILIARIA SUCRE SPA`, nombres de ejecutivos, comuna, dirección
ni coincidencia difusa. La resolución de leads V2 se ejecuta sobre la maestra
completa y el filtro SUCRE se aplica después, por código resuelto.

### Arquitectura

```text
webhook.py (FastAPI existente)
  └─ owner_portal/router.py
       ├─ security.py       CRM auth existente o gate local explícito
       ├─ service.py         lecturas/proyecciones y reglas explicables
       ├─ analytics.py       contrato futuro de eventos, sin persistencia
       └─ templates/owner_portal_preview.html
```

Las rutas locales son `/owner-portal-preview` (selección automática) y
`/owner-portal-preview/{property_code}` (validación de un código). No son
rutas públicas anónimas ni existen tokens de producción. El servicio es
read-only y no importa ni ejecuta modelos de machine learning.

### Datos y límites de la vista

- Identidad, precio actual, características, publicaciones e imágenes se
  muestran únicamente desde campos verificados y proyectados.
- Leads: solo enlaces `EXACT_CANONICAL` o `EXACT_ALIAS` de V2; conflictos,
  ambiguos y no enlazados quedan fuera del alcance SUCRE.
- Mercado: comparables descriptivos por comuna, tipo y operación, con precio y
  superficie válidos y fecha reciente cuando existe. `mercado_comunal` aporta
  contexto agregado; no se presenta una tasación ni una recomendación
  automática.
- Historial: la evolución no se dibuja cuando no hay suficientes puntos
  históricos. `tasaciones_final` no se usa como valor automático.
- No existe fuente confiable ni contrato para pageviews, impresiones o CTR.
  Los registros de `visitas` actuales prueban intención, autorización o firma,
  pero no asistencia completada; por eso V1 muestra esa métrica como no
  disponible.

### Seguridad, instrumentación y fases futuras

El propietario no usa directamente el login del CRM en el diseño final. En
esta fase la preview exige el usuario autenticado existente o
`OWNER_PORTAL_PREVIEW_DEV_MODE=true` desde localhost. No se implementan token
público, OTP, firma, autorización o modificación de precio, emails ni
scheduler.

El contrato futuro de eventos es:
`portal_opened`, `market_section_viewed`, `price_history_viewed`,
`pricing_recommendation_viewed`, `price_authorization_started`,
`price_authorized`, `price_rejected` y `contact_executive_clicked`, con
`event_name`, `property_code`, `event_at`, `session_id`, `portal_token_id`
futuro y metadata mínima permitida. El prototipo no persiste eventos.

En una fase posterior, Predictive Analytics podría construir
`property_week`, ventanas rolling, targets y entrenamiento con separación
temporal. Esta fase solo entrega Commercial Analytics, Pricing Intelligence,
Decision Support y Data Instrumentation descriptiva; no hace inferencia ni
recomendación ML.
