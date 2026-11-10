# Acceptance run: TARGETED_PASS

- Mode: `real_deepseek_api` (DeepSeek real API)
- Commit: `acc8f84e`
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
| 1 | ¿Me puedes mostrar otras propiedades en La Florida con un precio parecido? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Revisé el listado con esos parámetros y, por ahora, no tengo otras propiedades en La Florida con un precio cercano a las UF 3.160 del código 12006.  Para no dejarte sin alternativas, puedo ampliar un poco la búsqueda: por ejemplo, sumar comunas vecinas como La Granja, San Ramón o Peñalolén, o subir levemente el rango de precio. ¿Cuál de esas dos opciones te acomoda más? Con eso te muestro las que aparezcan y las revisamos juntos. | PASS |
| 2 | ¿Podrías buscar opciones en Macul? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Busqué en Macul con un rango de precio similar al código 12006 y no me aparecen opciones disponibles por ahora. Puede ser que el stock en esa comuna esté acotado o que el presupuesto quede justo para lo que hay publicado.  ¿Te parece si amplío un poco la búsqueda? Puedo sumar comunas cercanas como Peñalolén, La Granja o San Joaquín, o mover el rango de precio levemente hacia arriba. Dime cuál prefieres y lo reviso al tiro. | PASS |

### synthetic-025 — NORMAL, CLP 161000000, property 12025

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Me interesa hacer una oferta por esta vivienda, ¿con quién la reviso? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | contacto_directo | La administración revisa las ofertas junto con el propietario. Si quieres presentar una, puedes compartir por este chat el monto y las condiciones para que la solicitud quede registrada y la evalúen; esto no confirma su aceptación ni un descuento adicional. | PASS |
| 2 | ¿Qué antecedentes necesito revisar sobre el financiamiento? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Para revisar el financiamiento de esta casa, lo principal es contar con una preaprobación o simulación hipotecaria que indique monto, plazo, tasa y dividendo estimado. También conviene revisar el pie disponible, tu capacidad de pago, la tasación que haga el banco y los gastos operacionales. Como referencia, el precio publicado es $161.000.000 y los gastos comunes son $75.000, así que ese costo mensual debe sumarse al dividendo. En la ficha no aparece si el propietario ofrece financiamiento directo; eso habría que verificarlo. Si quieres, puedes compartir tu preaprobación o simulación para dejar el antecedente. | PASS |

### synthetic-026 — COLD, UF 2650, property 12026

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Pueden buscar propiedades en otra comuna, por ejemplo La Florida? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Claro, revisé en La Florida y por ahora no tengo opciones nuevas en venta que calcen con lo que buscas. Puede pasar que el stock se mueva rápido, así que vale la pena dejar la búsqueda bien afinada.  ¿Te sirve si ampliamos un poco? Por ejemplo, mirar en comunas cercanas o ajustar el presupuesto o la cantidad de dormitorios, y así te muestro alternativas concretas. Cuéntame qué es lo que no puede faltar para ti y lo reviso.  De paso, la propiedad 12026 sigue disponible en venta con el precio actualizado a UF 2.650, por si te interesa retomarla. | PASS |
| 2 | ¿Hay otra propiedad disponible en Macul? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Revisé en Macul y por ahora no tengo otras propiedades en venta que calcen con esa búsqueda. El stock se mueve rápido, así que puede aparecer algo pronto.  ¿Te sirve si ampliamos un poco? Podemos mirar en comunas cercanas, o ajustar presupuesto y cantidad de dormitorios para mostrarte alternativas concretas. Cuéntame qué priorizas y lo reviso.  De paso, la propiedad 12026 en La Florida sigue disponible en venta a UF 2.650, por si quieres retomarla. | PASS |

### synthetic-027 — HOT, CLP 128000000, property 12027

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Esta vivienda no me acomoda; ¿me muestran alternativas por La Reina? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Entiendo perfectamente, no hay problema. Revisé en La Reina, en venta, y en este momento no tengo otras propiedades que calcen con esa búsqueda.  Para no dejarte sin opciones, ¿te parece si ampliamos un poco el rango? Puedo mirar en comunas cercanas como Ñuñoa, Peñalolén o Las Condes, o bien ajustar el presupuesto para ver si aparece algo dentro de La Reina. Cuéntame cuál de esas dos alternativas te acomoda más y lo reviso al tiro. | PASS |
| 2 | Me gustaría ver otra comuna, como Las Condes. | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Revisé en Las Condes, en venta, y en este momento no me aparece una alternativa que calce con esa búsqueda.  Para no dejarte con las manos vacías, ¿te parece si ampliamos un poco los filtros? Puedo mirar en comunas cercanas o ajustar el rango de presupuesto, y así te muestro opciones que se acerquen a lo que buscas. Cuéntame por dónde prefieres que amplíe y lo reviso. | PASS |

### synthetic-036 — HOT, UF 3500, property 12036

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Busco un departamento en La Florida; ¿tienen algo similar? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Claro, te confirmo primero que la propiedad 12036 sigue publicada en UF 3.500, que es su precio actual tras la rebaja desde UF 3.600.  Sobre algo similar, te cuento con honestidad: hoy no tengo otras alternativas publicadas en La Florida en venta que calcen con ese perfil. Para no dejarte con las manos vacías, ¿te sirve que ampliemos un poco la búsqueda? Podría ser mirando comunas cercanas, ajustando el presupuesto o viendo otro tipo de propiedad. Dime qué prefieres priorizar y lo reviso. | PASS |
| 2 | Estoy buscando algo por Macul. | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Te agradezco el dato. Revisé en Macul y, siendo franco, hoy no tengo departamentos en venta publicados que calcen con esa búsqueda.  Para no dejarte sin opciones, ¿te parece si ampliamos un poco? Podemos mirar comunas cercanas a Macul, o bien ajustar el presupuesto. Cuéntame qué prefieres priorizar y lo reviso con gusto. | PASS |
