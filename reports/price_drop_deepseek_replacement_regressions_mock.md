# Acceptance run: TARGETED_SIMULATED

- Mode: `simulated_mock` (Synthetic response stub)
- Commit: `35f6450e97d005f57041363349a8c88e9acdeacb`
- Conversations / inbound turns: 5 / 10
- Targeted scenarios: synthetic-006, synthetic-025, synthetic-026, synthetic-027, synthetic-036
- Real DeepSeek replies: 0
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
| 1 | ¿Me puedes mostrar otras propiedades en La Florida con un precio parecido? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Busqué propiedades en La Florida con esos criterios y no encontré coincidencias disponibles por ahora. Si quieres, puedo ampliar la búsqueda o ajustar algún criterio. | PASS |
| 2 | ¿Podrías buscar opciones en Macul? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Busqué propiedades en Macul con esos criterios y no encontré coincidencias disponibles por ahora. Si quieres, puedo ampliar la búsqueda o ajustar algún criterio. | PASS |

### synthetic-025 — NORMAL, CLP 161000000, property 12025

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Me interesa hacer una oferta por esta vivienda, ¿con quién la reviso? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | consulta_general | La oferta debe evaluarla administración con el propietario; no puedo confirmar descuentos adicionales. Si quieres avanzar, comparte por este chat el monto y las condiciones para registrar la solicitud; eso no supone que sea aceptada. | PASS |
| 2 | ¿Qué antecedentes necesito revisar sobre el financiamiento? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | El precio vigente de la propiedad 12025 es $161.000.000.. La ficha conserva los datos verificados y puedo aclarar cualquier detalle. | PASS |

### synthetic-026 — COLD, UF 2650, property 12026

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Pueden buscar propiedades en otra comuna, por ejemplo La Florida? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Busqué propiedades en La Florida con esos criterios y no encontré coincidencias disponibles por ahora. Si quieres, puedo ampliar la búsqueda o ajustar algún criterio. | PASS |
| 2 | ¿Hay otra propiedad disponible en Macul? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Busqué propiedades en Macul con esos criterios y no encontré coincidencias disponibles por ahora. Si quieres, puedo ampliar la búsqueda o ajustar algún criterio. | PASS |

### synthetic-027 — HOT, CLP 128000000, property 12027

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Esta vivienda no me acomoda; ¿me muestran alternativas por La Reina? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Busqué propiedades en La Reina con esos criterios y no encontré coincidencias disponibles por ahora. Si quieres, puedo ampliar la búsqueda o ajustar algún criterio. | PASS |
| 2 | Me gustaría ver otra comuna, como Las Condes. | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Busqué propiedades en Las Condes con esos criterios y no encontré coincidencias disponibles por ahora. Si quieres, puedo ampliar la búsqueda o ajustar algún criterio. | PASS |

### synthetic-036 — HOT, UF 3500, property 12036

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Busco un departamento en La Florida; ¿tienen algo similar? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Busqué propiedades en La Florida con esos criterios y no encontré coincidencias disponibles por ahora. Si quieres, puedo ampliar la búsqueda o ajustar algún criterio. | PASS |
| 2 | Estoy buscando algo por Macul. | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Busqué propiedades en Macul con esos criterios y no encontré coincidencias disponibles por ahora. Si quieres, puedo ampliar la búsqueda o ajustar algún criterio. | PASS |
