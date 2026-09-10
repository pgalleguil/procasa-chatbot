# Portal del Propietario PROCASA SUCRE — contrato V1

Este documento formaliza la vista de lectura del portal antes de habilitar
acceso público, tokens, emails o autorización de cambios de precio.

## DTO de exposición

El router obtiene un único `as_of` con zona `America/Santiago` y entrega al
template únicamente `OwnerPortalPropertyViewV1.to_dict()`. El objeto no
contiene documentos Mongo completos, PII ni campos internos del CRM.

| Bloque | Campos | Fuente |
|---|---|---|
| Identidad | `property_code`, `property_type`, `operation`, `commune`, `region` | `universo_cartera_prop360` |
| Presentación | `main_image_url`, `current_price_uf`, `current_price_clp`, `bedrooms`, `bathrooms`, `parking`, `built_area_m2`, `land_area_m2` | Maestro Prop360 + `propiedades_captacion` para imagen |
| Actividad comercial | `inquiries_previous_7d`, `inquiries_previous_30d` | `leads` + resolver V2; solo `EXACT_CANONICAL`/`EXACT_ALIAS` |
| Mercado | `comparable_count`, `market_median_uf`, `market_low_uf`, `market_high_uf`, `market_uf_m2`, `market_data_available`, `market_as_of` | `propiedades_captacion` + `mercado_comunal` |
| Precio | `current_price`, `previous_price`, `last_price_change_at` | Maestro actual + snapshot/historial verificable |
| Metadata | `as_of`, `data_updated_at`, `data_quality`, `recommendation` | Cutoff único; `recommendation` permanece `null` |

La plantilla recibe solamente este DTO serializado. El adaptador
`build_owner_portal_view` existe solo para compatibilidad con callers internos
anteriores y no se utiliza para renderizar la ruta.

## Fuentes y reglas

- La propiedad debe cumplir exactamente `estado.oficina == "PROCASA SUCRE"` y
  estar activa en `estado.disponible_prop360`.
- El servicio consolida propiedad, consultas, mercado e historial en una
  operación `get_owner_portal_property_view(db, property_code, as_of)`.
- Las consultas usan el mismo cutoff en 7 y 30 días. `CONFLICT`, `AMBIGUOUS` y
  `UNMATCHED` quedan excluidos.
- El mercado requiere al menos cinco comparables con misma comuna, tipo y
  operación, precio UF finito y mayor que cero, superficie finita y mayor que
  cero, y fecha no anterior a un año ni igual/futura respecto del cutoff cuando
  existe. No se convierte CLP a UF.
- Con cero a cuatro comparables, `market_data_available=false` y las métricas
  estadísticas son `null`.
- No se aplican winsorization, IQR trimming, percentiles ni ML. La auditoría de
  distribución/outliers se reporta fuera del DTO para decisión posterior.

## PII y allowlist

El DTO excluye propietario, email, teléfono, RUT, nombre, dirección exacta,
mensajes, notas CRM, datos de ejecutivos e IDs Mongo innecesarios. La
serialización se valida mediante `assert_owner_portal_payload_allowlisted`.

## Historial y recomendación

`previous_price` y `last_price_change_at` solo aparecen cuando existe evidencia
verificable. Un único cambio no se convierte en una serie histórica ni en un
gráfico. `recommendation` es siempre `null` en V1.

## Eventos futuros: `OwnerPortalEventV1`

Campos: `event_name`, `property_code`, `event_at_utc`, `event_at_local`,
`session_id`, `portal_token_id`, `schema_version`, `metadata`.

Eventos permitidos:

```text
portal_opened
market_section_viewed
price_history_viewed
pricing_recommendation_viewed
price_authorization_started
price_authorized
price_rejected
contact_executive_clicked
```

`metadata` se valida por evento y solo acepta claves escalares allowlisted. No
se aceptan diccionarios arbitrarios ni PII. No existe colección ni persistencia
de eventos en esta fase.

## Funnel futuro

```text
email_sent
→ portal_opened
→ market_section_viewed
→ pricing_recommendation_viewed
→ price_authorization_started
→ price_authorized / price_rejected
→ post_change_inquiries
→ visits
→ closed_operation
```

Actualmente no existen en este portal `email_sent`, autorización, post-change
inquiries, visitas verificadas ni operación cerrada. Tampoco se implementa el
experimento control/treatment:

- Control: solicitud tradicional de baja vía email.
- Treatment: email → portal → evidencia de mercado → solicitud.

Las métricas futuras serán open rate, authorization rate, time-to-response,
adjustment accepted e inquiries after adjustment.

## Acceso futuro

`OwnerPortalAccessV1` queda definido con `token_id`, `property_code`,
`issued_at`, `expires_at`, `revoked_at`, `status`, `purpose`, `created_by` y
`token_hash`. El token raw no es un campo del schema y no se almacena. La
duración final aún no está definida.

Los permisos conceptuales son independientes:

- `VIEW_PROPERTY`
- `AUTHORIZE_PRICE_CHANGE`

Un enlace de visualización nunca concede automáticamente autorización de
cambio de precio.

## Performance y query audit

El benchmark se ejecutó contra Mongo real sobre el Caso A (`6786`), con tres
calentamientos no medidos y veinte solicitudes secuenciales medidas. La versión
anterior cargaba la identidad maestra completa (1.965 documentos), leía toda
`leads` (1.232 documentos observados en la auditoría) y hacía `COLLSCAN` sobre
`propiedades_captacion`.

| Métrica | Antes | Optimizado |
|---|---:|---:|
| Mínimo | — | 591,5 ms |
| P50 | 2.906 ms | 599,7 ms |
| P90 | 27.078 ms | 618,1 ms |
| P95 | — | 626,2 ms |
| Máximo | 43.179 ms | 673,6 ms |
| Media | — | 606,6 ms |
| Consultas por request | 6 cálidas / 26 fría | 7 |

La versión optimizada consulta la propiedad por `codigo`, obtiene leads sólo
por identificadores verificados y namespaced del anuncio, y trae únicamente
propiedades maestras necesarias para resolver conflictos de esos candidatos.
La ventana temporal se conserva en Python porque `leads.created_at` contiene
strings ISO con offsets mixtos, un valor naive y faltantes; así se preserva
`parse_aware_datetime` y `is_in_previous_window` sin cambiar la semántica V2.

| Consulta | Explain antes | Explain después |
|---|---|---|
| Propiedad `codigo=6786` + oficina | `IXSCAN`, 1 doc / 1 key | `IXSCAN`, 1 doc / 1 key |
| Mercado comuna + tipo | `IXSCAN`, 1 doc / 1 key | `IXSCAN`, 1 doc / 1 key |
| Leads por identificadores | `COLLSCAN`, 1 retorno / 1.232 docs / 0 keys | `OR` + `IXSCAN`, 1 retorno / 1 doc / 9 keys |
| Comparables comuna + tipo + operación | `COLLSCAN`, 19.357 docs / 0 keys | `IXSCAN`, 0 docs / 0 keys |

Índices nuevos justificados por el explain:

- `leads.prospecto.codigo`
- `leads.prospecto.codigo_mercadolibre`
- `leads.prospecto.codigo_yapo`
- `leads.prospecto.codigo_propiedad`
- `leads.prospecto.propiedad_codigo`
- `propiedades_captacion.(comuna, tipo_propiedad, operacion)`

El índice único existente `mercado_comunal.(comuna, tipo_propiedad)` ya cubría
el agregado y no se duplicó. El Caso A no tiene comparables que cumplan el
grupo solicitado; por eso su resultado sigue siendo `comparables=0` y no hay
distribución estadística evaluable.

La auditoría específica del Caso A encontró cero filas de comparables, por lo
que su distribución observada es `n=0` y no existen outliers evaluables para
ese caso. Un barrido global de `propiedades_captacion` se agotó por timeout de
Mongo durante esta validación y no se usa como evidencia estadística.
