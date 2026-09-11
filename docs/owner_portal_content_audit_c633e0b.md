# Auditoría de contenido visible — caso 6464

Fecha de revisión: 11/09/2026  
Colección de cartera: `universo_cartera_prop360`  
Comparables: `propiedades_captacion`  
Mercado agregado: `mercado_comunal`

| Elemento visible | Fuente | Corte | Calidad | Mantener |
|---|---|---|---|---|
| Código, tipo, comuna, precio, superficie y características | `universo_cartera_prop360` | Actualización propia de la ficha | Alta cuando el campo está presente y validado | Sí |
| Consultas 7/30/90 días | `leads` + linkage exacto de propiedad | Fecha de generación del informe | Alta solo para enlaces canónicos o alias verificados | Sí |
| Portales activos | `universo_cartera_prop360.publicaciones` | Estado publicado/activo actual | Alta si existe señal explícita de publicación | Sí |
| Historial de precio | `pricing_intelligence_property_snapshots_v1` | Fecha del snapshot anterior | Condicional; requiere precio y fecha anteriores | Solo si existe |
| UF/m² publicado | `mercado_comunal.mercado_venta.uf_m2_publicacion_actual` | 28/04/2026 | Media; agregado del informe comunal | Sí, con semántica publicada |
| Variación 12 meses | `mercado_comunal.mercado_venta.variacion_uf_m2_12m` | 28/04/2026 | Media; variación reportada del agregado | Sí, sin causalidad |
| Publicaciones activas observadas | `mercado_comunal.mercado_venta.publicaciones_activas` | 28/04/2026 | Media; avisos observados, no necesariamente propiedades únicas | Sí, con aclaración |
| Mediana y percentiles de precio | `propiedades_captacion` | Fecha propia de cada fila; cohorte limitada a 365 días | Alta como distribución de publicaciones válidas | Sí |
| Ejemplos comparables | `propiedades_captacion` | Fecha de publicación; si falta, última actualización observada | Alta si precio, superficie y portal existen; anónimos | Sí |
| UF/m² efectivo | Agregado comunal sin trazabilidad fila a fila | 28/04/2026 | Insuficiente para presentarlo como cierre o transacción | No |
| Indicadores nacionales | Snapshots internos / `uf_cache` | Fecha propia de cada snapshot | Solo si hay al menos dos indicadores nacionales válidos | Condicional |

## Decisiones

- `mercado_comunal` y `propiedades_captacion` mantienen fechas y semánticas separadas.
- “Publicaciones comparables observadas” es la terminología visible; no se usa “transacciones”.
- No se muestra “precio efectivo”, “valor transado” ni “precio de cierre” sin evidencia fila a fila.
- La narrativa local y la lectura comercial son determinísticas y descriptivas; no atribuyen causalidad.
