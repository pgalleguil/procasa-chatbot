# Auditoría técnica de mercado — convergencia Owner Intelligence

Fecha de auditoría: 10/09/2026  
Colección principal de cartera: `universo_cartera_prop360`  
Colección de mercado: `mercado_comunal`  
Alcance: lectura interna, sin escrituras MongoDB.

## Alcance y corte

La cartera contiene 1.965 documentos, de los cuales 459 corresponden a `estado.oficina = PROCASA SUCRE`. `mercado_comunal` contiene 123 combinaciones comunas/tipo de propiedad. Para el caso principal seleccionado automáticamente (`6464`, Casa, Santiago), el corte comunal disponible es 28/04/2026 y la muestra comparable vigente devuelve 59 propiedades similares, con 256 filas válidas en el universo amplio.

## Matriz de campos

| Campo | Definición operativa | Cobertura auditada | Utilidad | Tratamiento visible |
|---|---|---:|---|---|
| `mercado_venta.uf_m2_publicacion_actual` | UF/m² publicado agregado para comuna/tipo | Disponible en el registro Santiago/Casa revisado | Señal protagonista de mercado local | Visible |
| `mercado_venta.variacion_uf_m2_12m` | Variación reportada del UF/m² publicado a 12 meses | Disponible en el registro Santiago/Casa revisado | Contexto temporal del corte | Visible |
| `mercado_venta.publicaciones_activas` | Avisos activos observados en el corte | Disponible en el registro Santiago/Casa revisado | Tamaño observado de la oferta; no implica propiedades únicas | Visible con semántica explícita |
| `mercado_venta.publicaciones_totales` | Total de publicaciones del informe | Disponible en el registro Santiago/Casa revisado | Control de escala del snapshot | Interno; no se duplica en la vista |
| `mercado_venta.uf_m2_venta_efectiva_actual` | Agregado reportado de venta efectiva | Disponible en el registro revisado | Contexto técnico, no transacción individual verificable | Interno; no se expone al propietario |
| `indicadores_mercado.nivel_competencia` | Etiqueta agregada de competencia | Disponible en el registro revisado | Contexto secundario | Interno en esta convergencia |
| `indicadores_mercado.liquidez` | Etiqueta agregada de liquidez | No se usa como señal protagonista | Evita presentar una etiqueta sin serie explicativa | No visible |
| `propiedades_captacion.precio_uf` + superficie | Publicaciones con precio y superficie válidos, deduplicadas por `listing_id` | 59 similares; 256 amplias para el caso 6464 | Percentiles y posición relativa | Visible |
| `pricing_intelligence_property_snapshots_v1` | Estado anterior fechado de precio | Disponible de forma condicional por propiedad | Permite comparar dos estados reales | Solo si hay estado anterior válido |

## Decisiones de producto

- Mercado local primero: UF/m² publicado, variación 12 meses, publicaciones observadas y muestra comparable son los cuatro indicadores máximos de la vista.
- La UF nacional no abre una sección propia cuando es el único snapshot nacional disponible.
- La venta efectiva agregada queda fuera de la UI porque su semántica no permite leerla como transacción individual.
- La fecha del corte de `mercado_comunal` se muestra separada de la fecha de actualización de la ficha.
- Los percentiles se calculan con la cohorte verificable; no se genera una recomendación ni una proyección.
