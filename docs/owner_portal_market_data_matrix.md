# Matriz de datos de mercado del Portal del Propietario

Auditoría realizada sobre `mercado_comunal` y su uso en el Portal del Propietario. La
cartera maestra actualmente utilizada es `universo_cartera_prop360`; la colección de
publicaciones comparables es `propiedades_captacion`.
La colección consultada contiene 123 documentos comunales. Para Santiago / Departamento,
el documento de referencia revisado tiene `source.fecha_reporte = 28/04/2026` y
`source.filename = santiago_departamento.pdf`.

| Indicador | Significado | Calidad | Utilidad propietario | Mostrar |
| --- | --- | --- | --- | --- |
| `mercado_venta.uf_m2_publicacion_actual` | Valor agregado publicado por m² en el informe comunal | Media: informe agregado proveniente de PDF | Alta para comparar el precio publicado en la misma unidad | Sí, como **UF/m² publicado** |
| `mercado_venta.uf_m2_venta_efectiva_actual` | Agregado denominado “efectivo” por el informe | Ambigua: el PDF no entrega transacciones individuales verificables | Riesgo alto de interpretarlo como precio real de cierre | No en la V1; queda con semántica explícita interna |
| `mercado_venta.variacion_uf_m2_12m` | Variación reportada del UF/m² publicado en 12 meses | Media: depende del informe y su corte | Alta como contexto temporal, sin causalidad | Sí, como **Variación · 12 meses** |
| `mercado_venta.publicaciones_activas` | Avisos activos observados en el corte | Media: no prueba unicidad entre portales | Alta para dimensionar el universo observado | Sí, como **Publicaciones activas observadas** |
| `mercado_venta.publicaciones_totales` | Volumen histórico de publicaciones del informe | Media | Baja para una lectura puntual | No |
| `mercado_venta.tendencia_publicaciones` | Categoría agregada de tendencia del informe | Media-baja | Media, pero puede simplificar demasiado la lectura | No en la V1 |
| `indicadores_mercado.liquidez` | Etiqueta agregada del informe | Media-baja; no se expone como medida operacional propia | Media, pero requiere definición y ventana | No |
| `indicadores_mercado.nivel_competencia` | Etiqueta agregada del informe | Media-baja; no es una medición independiente del portal | Media, pero no necesaria para esta lectura | No |
| `indicadores_mercado.presion_baja_precio`, `score_presion_comercial` | Señales derivadas del informe | No suficientemente auditadas para una interpretación causal | Riesgo de convertirse en recomendación | No |
| `indicadores_mercado.cap_rate`, `payback_anios`, `brecha_publicacion_vs_cierre_pct` | Indicadores de inversión o brecha agregada | Variable; no representan el caso individual | Baja para el recorrido V1 | No |
| `mercado_arriendo.*` | Referencias de arriendo | Fuera de contexto para una propiedad en venta | Baja en este portal de venta | No |

## Decisión de presentación

La sección local muestra cuatro elementos: UF/m² publicado, variación a 12 meses,
publicaciones activas observadas y propiedades similares utilizadas. El corte se muestra
separado de la fecha de actualización de la propiedad.

`uf_m2_venta_efectiva_actual` permanece disponible en el DTO para trazabilidad, pero no
se renderiza como “UF/m² efectivo”. La evidencia revisada indica que estos informes son
agregados heurísticamente extraídos de PDFs y gráficos, no un registro fila a fila de
compraventas cerradas.

`publicaciones_activas` se presenta como publicaciones observadas, no como propiedades
únicas. La auditoría complementaria de `propiedades_captacion` para Santiago / Departamento /
venta encontró 1.544 documentos y 1.544 `listing_id` distintos; ese hallazgo no convierte
el conteo comunal histórico en un conteo universal de propiedades únicas entre portales.
