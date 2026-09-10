# Portal del Propietario PROCASA SUCRE — contrato de lectura

Este documento describe la vista local de sólo lectura del informe “Informe
Digital de Gestión, Mercado y Desempeño Inmobiliario — PROCASA SUCRE”. No
habilita acceso público, tokens, emails, autorización de cambios de precio,
eventos persistidos ni modelos de machine learning.

## DTO único de exposición

El router calcula un único `as_of` con zona `America/Santiago` y entrega al
template exclusivamente `OwnerPortalPropertyViewV1.to_dict()`. La forma es
PII-free y se valida con `assert_owner_portal_payload_allowlisted`.

| Bloque | Campos principales | Fuente |
|---|---|---|
| Identidad y hero | `property_code`, `property_type`, `operation`, `commune`, `region`, `main_image_url`, superficies y características | `universo_cartera_prop360` + capturas de `propiedades_captacion` |
| Resumen | precio actual, consultas 7/30/90 días, superficie y publicaciones activas | Maestro + resolver de consultas + publicaciones verificadas |
| Presencia | `publications` con portal, URL segura y fechas cuando existen | Maestro de publicaciones + captura asociada |
| Actividad | `activity_series` semanal de hasta 12 semanas | Consultas vinculadas exactamente y sus fechas reales |
| Línea de tiempo | `timeline` de publicaciones, cambios de precio, actualizaciones y consultas | Registros fechados ya verificados |
| Chile | `national_indicators`, `national_context_note`, `market_intelligence_snapshot` | Snapshots internos; enlaces institucionales de contexto |
| Comuna | `local_context`, nota de alcance regional | `mercado_comunal` + comparables locales |
| Comparables | `comparable_cohort`, `positioning`, percentiles P10/P25/mediana/P75/P90 | `propiedades_captacion` validado en Python |
| Precio e integridad | `current_price`, `previous_price`, `last_price_change_at`, `data_quality` | Maestro + snapshots de precio |

`recommendation` permanece `null` y no se construye ningún objeto de
pronóstico en esta fase. El contrato futuro `DemandForecastViewV1` sólo está
definido para una integración posterior y no se instancia ni se renderiza.

## Alcance y reglas de seguridad

- La propiedad debe cumplir exactamente `estado.oficina == "PROCASA SUCRE"` y
  estar activa en `estado.disponible_prop360`.
- La selección automática prioriza una propiedad activa con fotografía, precio,
  comuna, tipo, superficie y características verificables. El código 6448 se
  conserva como caso fallback visual sin fotografía.
- Una publicación sólo aparece si su registro tiene `publicada == true` o un
  estado explícito activo/publicado. Las URLs se exponen únicamente cuando son
  `http` o `https`.
- Una consulta cuenta sólo por código canónico o identificador de publicación
  verificado. `CONFLICT`, `AMBIGUOUS` y `UNMATCHED` se excluyen; nunca se
  muestran nombres, emails, teléfonos, mensajes ni otros datos personales.
- La serie semanal se oculta si no existe al menos una fecha válida dentro de
  las últimas doce semanas. No se rellenan puntos con datos sintéticos.

## Arquitectura de inteligencia de mercado

```text
fuentes externas/públicas
        ↓
ingesta manual o periódica
        ↓
MarketIntelligenceSnapshotV1
        ↓
Owner Portal (render cliente desde el DTO)
```

La fase actual consume snapshots ya disponibles; no agrega scheduler, job ni
llamada externa por pageview. La UF se lee desde `uf_cache`. Las fuentes
institucionales de Banco Central, INE y MINVU quedan enlazadas como contexto,
sin inventar una cifra que no esté persistida en un snapshot fechado.

## Mercado local y comparables

El mercado local se busca por comuna y tipo con coincidencia exacta
normalizada, incluyendo una alternativa exacta por `match_key` para acentos y
mayúsculas del legado. La operación se limita a venta en esta vista. Se
aceptan precio UF y superficie positivos y finitos; la superficie usa el orden
`superficie`, `superficie_construida`, `m2_construidos`, `m2_totales`. Las fechas
fuera del año de observación o posteriores al corte se excluyen; una fecha
ausente puede conservarse y queda documentada en la regla de fecha.

La jerarquía visible exige `N >= 8`:

1. `Alta similitud`: superficie ±20%, dormitorios equivalentes y baños
   comparables, con fecha válida.
2. `Propiedades similares`: superficie ±25% y dormitorios iguales o ±1 cuando
   existen ambas dimensiones.
3. `Mercado amplio`: misma comuna, tipo y operación, con precio y superficie
   válidos.

Se selecciona el primer nivel que alcanza ocho publicaciones. El DTO conserva
  los tamaños de los tres grupos para que no haya eliminación silenciosa. La
  visualización principal usa P10, P25, mediana, P75 y P90; no usa mínimo ni
  máximo como eje principal. La posición de la propiedad es descriptiva, no
  una tasación, una causalidad ni una recomendación.

## Evolución de precio y recomendaciones

`previous_price` y `last_price_change_at` sólo se exponen con evidencia
verificable. La línea de tiempo no muestra datos de personas interesadas y
limita las consultas a un evento agregado por fecha.

El informe no incluye tarjetas vacías de forecast o recomendación. No existe
acción de autorización ni escritura en MongoDB. `OwnerPortalEventV1` y
`OwnerPortalAccessV1` siguen definidos para fases posteriores, pero no se
persisten ni se activan aquí.

## Performance

El render usa el DTO ya ensamblado: los gráficos son SVG/DOM inline y no
generan queries. Para evitar repetir el barrido de comparables dentro de una
ventana breve, el servicio mantiene un cache de proceso de 30 segundos por
comuna/tipo/fecha de corte; no modifica MongoDB ni sus índices. El benchmark
local se ejecutó sobre el preview con tres calentamientos y diez solicitudes
secuenciales medidas por código. El P90 fue 642,5 ms para 6786 y 702,3 ms para
6448; ambas lecturas quedan bajo el objetivo de 1.500 ms en esta medición
local.
