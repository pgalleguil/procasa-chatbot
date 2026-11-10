# Acceptance run: TARGETED_PASS

- Mode: `real_deepseek_api` (DeepSeek real API)
- Commit: `35f6450e97d005f57041363349a8c88e9acdeacb`
- Conversations / inbound turns: 5 / 10
- Targeted scenarios: synthetic-006, synthetic-025, synthetic-026, synthetic-027, synthetic-036
- Real DeepSeek replies: 10
- Mock customer/admin deliveries: 10 / 5
- Safety: Mongo URI loaded `False`, real WhatsApp calls `0`, merged `False`

## Evaluated checks

| Check | Passed |
|---|---:|
| Property identity | 10 |
| Numeric property facts | 10 |
| Price claims (exact currency/current price on price questions) | 10 |
| Naturalness heuristic (format/length only) | 10 |
| Final response semantic relevance | 10 |
| Alternative request and commune relevance | 10 |
| Offer routing answered | 10 |
| V2 context and multi-turn continuity | 10 |
| Campaign classification | 10 |
| No unsolicited visit offer | 10 |
| Inbound idempotency | 10 |
| Queue idempotency | 10 |
| Prepared temperature snapshot | 10 |
| Customer inbound message log | 10 |
| V2 campaign reply event audit log | 10 |
| Customer-facing outbound message log | 10 |
| Structured model responses | 10 |
| Model fallback after structured error | 0 |

## Issues

None.

## Conversation matrix

### synthetic-006 — HOT, UF 3160, property 12006

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Me puedes mostrar otras propiedades en La Florida con un precio parecido? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Claro, entiendo. Sobre la 12006 te confirmo que su precio actual es UF 3.160, ya con la rebaja aplicada.  Respecto a otras alternativas en La Florida en ese mismo rango, ahora no tengo opciones publicadas que calcen exactamente con la búsqueda. Para no mostrarte cosas que no te sirvan, prefiero preguntarte: ¿te acomoda ampliar un poco el rango de precio o quizá mirar también una comuna cercana? Con eso hago una nueva búsqueda y te muestro lo que sí tengamos disponible. | PASS |
| 2 | ¿Podrías buscar opciones en Macul? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Revisé en Macul y ahora no tengo opciones publicadas en venta que calcen con lo que buscas, así que prefiero no mostrarte algo que no te sirva.  Para encontrar alternativas reales, ¿te acomoda ampliar un poco el rango de precio respecto a las UF 3.160 de la 12006, o prefieres que mire en alguna comuna cercana como Ñuñoa o San Joaquín? Con eso hago una nueva búsqueda y te muestro lo que sí tengamos disponible. | PASS |

### synthetic-025 — NORMAL, CLP 161000000, property 12025

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Me interesa hacer una oferta por esta vivienda, ¿con quién la reviso? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | contacto_directo | La oferta debe evaluarla administración con el propietario; no puedo confirmar descuentos adicionales. Si quieres avanzar, comparte por este chat el monto y las condiciones para registrar la solicitud; eso no supone que sea aceptada. | PASS |
| 2 | ¿Qué antecedentes necesito revisar sobre el financiamiento? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Te cuento con transparencia: la ficha de esta casa no incluye condiciones ni simulaciones de financiamiento, así que no puedo entregarte tasas, plazos ni pie estimado.  Lo que sí conviene tener a la vista es tu preaprobación o evaluación hipotecaria del banco, tu capacidad de endeudamiento y el pie disponible. Como referencia, el precio publicado es $161.000.000 y los gastos comunes son $75.000; las contribuciones no aparecen en la ficha. Con esos antecedentes el ejecutivo puede orientarte mejor cuando revises la operación. | PASS |

### synthetic-026 — COLD, UF 2650, property 12026

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Pueden buscar propiedades en otra comuna, por ejemplo La Florida? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Claro, sobre el 12026 te confirmo que el precio vigente es UF 2.650, rebajado desde UF 2.750, tal como salió en la comunicación.  Respecto a La Florida, revisé la búsqueda en venta y no tengo opciones disponibles que calcen hoy con esos filtros. Si te parece, puedo ampliar un poco la búsqueda: por ejemplo sumar comunas cercanas como Macul, Peñalolén, San Joaquín o Ñuñoa, o ajustar el presupuesto. ¿Cuál de esas dos alternativas prefieres que pruebe primero? | PASS |
| 2 | ¿Hay otra propiedad disponible en Macul? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Revisé la búsqueda de venta en Macul y no tengo otras opciones que calcen con esos filtros por ahora. El 12026 que ya viste sigue disponible en La Florida, con su precio vigente de UF 2.650.  Si quieres, amplío un poco la búsqueda: puedo sumar comunas cercanas como Peñalolén, San Joaquín o Ñuñoa, o bien ajustar el presupuesto o la cantidad de dormitorios para ampliar las alternativas. ¿Cuál de esas opciones prefieres que pruebe primero? | PASS |

### synthetic-027 — HOT, CLP 128000000, property 12027

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Esta vivienda no me acomoda; ¿me muestran alternativas por La Reina? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Entiendo perfectamente, y gracias por decírmelo. Revisé alternativas en venta en La Reina y ahora mismo no tengo opciones nuevas que calcen con lo que buscas; prefiero decírtelo con transparencia antes que mostrarte algo que no te acomode.\n ¿Te parece si ampliamos un poco la búsqueda? Puede ser mirar comunas cercanas como Peñalolén, Ñuñoa o Las Condes, o bien ajustar el tipo de propiedad o el presupuesto que tienes en mente. Con eso puedo buscar opciones que realmente tengan sentido para ti y mostrártelas con calma. | PASS |
| 2 | Me gustaría ver otra comuna, como Las Condes. | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Gracias por la aclaración. Revisé alternativas en venta en Las Condes y ahora mismo no me aparece ninguna opción nueva que calce con lo que buscas, así que prefiero decírtelo con honestidad antes que mostrarte algo que no corresponda.  ¿Te parece si ampliamos un poco la búsqueda? Podemos mirar comunas cercanas como Ñuñoa, Peñalolén o Providencia, o bien ajustar el tipo de propiedad o el presupuesto que tienes en mente. Cuéntame qué prefieres y con eso vuelvo a buscar opciones que realmente te hagan sentido. | PASS |

### synthetic-036 — HOT, UF 3500, property 12036

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Busco un departamento en La Florida; ¿tienen algo similar? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Claro, entiendo. La propiedad 12036 sigue publicada en venta a UF 3.500. Para algo similar en La Florida, con los filtros exactos de departamento en venta, no me aparecen nuevas opciones disponibles en este momento.  Si quieres, puedo ampliar un poco la búsqueda: por ejemplo, revisar comunas cercanas o ajustar el rango de precio o los dormitorios. ¿Prefieres que mantenga La Florida y ampliemos el presupuesto, o que incluya comunas cercanas? | PASS |
| 2 | Estoy buscando algo por Macul. | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Gracias por contarme. Busqué departamentos en venta en Macul y, con esos filtros, no me aparecen opciones nuevas disponibles en este momento.  Para no dejarte con las manos vacías, ¿te parece si ampliamos un poco la búsqueda? Podemos revisar comunas cercanas como Ñuñoa o La Florida, o bien ajustar el presupuesto o la cantidad de dormitorios. Cuéntame cuál de esas opciones prefieres y lo reviso enseguida. | PASS |
