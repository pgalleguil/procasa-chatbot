# Acceptance run: FAIL

- Mode: `real_deepseek_api` (DeepSeek real API)
- Commit: `acc8f84e`
- Conversations / inbound turns: 40 / 80
- Targeted scenarios: none
- Real DeepSeek replies: 72
- Mock customer/admin deliveries: 72 / 24
- Safety: Mongo URI loaded `False`, real WhatsApp calls `0`, merged `False`

## Evaluated checks

| Check | Passed |
|---|---:|
| Property identity | 80 |
| Numeric property facts | 80 |
| Price claims (exact currency/current price on price questions) | 80 |
| Naturalness heuristic (format/length only) | 80 |
| Final response semantic relevance | 61 |
| Alternative request and commune relevance | 64 |
| Offer routing answered | 77 |
| V2 context and multi-turn continuity | 80 |
| Campaign classification | 80 |
| No unsolicited visit offer | 80 |
| Inbound idempotency | 80 |
| Queue idempotency | 80 |
| Prepared temperature snapshot | 80 |
| Structured model responses | 72 |
| Model fallback after structured error | 0 |

## Issues

- `final_response_semantics:ALTERNATIVE_REQUEST_ANSWERED`
- `final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED`
- `final_response_semantics:NO_ORIGINAL_COMMUNE_SUBSTITUTION`
- `final_response_semantics:OFFER_HANDLER_ANSWERED`

## Conversation matrix

### synthetic-000 — HOT, UF 2650, property 12000

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Cómo queda el precio del código 12000 después de la rebaja? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Precio: UF 2.650 (referencia $103.350.000) | PASS |
| 2 | ¿Qué gastos comunes aparecen en la ficha? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Los gastos comunes que aparecen en la ficha del código 12000 son de $70.000. Si te sirve, también puedo repasar contigo el precio actual o los metrajes publicados. | PASS |

### synthetic-001 — NORMAL, CLP 128000000, property 12001

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Sí me interesa la propiedad 12001, pero no quiero que me llamen. ¿Qué detalle puedes contarme? | INTERES_DECLARADO / INTERES_DECLARADO | consulta_general | Quedo tranquila, dejamos todo por este canal escrito, sin llamadas.  Sobre la propiedad 12001 en Providencia: es una casa de 2 dormitorios y 2 baños, con 46 m² útiles y 51 m² totales, más 1 estacionamiento. La ficha indica orientación sur y gastos comunes de $75.000. El precio actual es $128.000.000, tras la rebaja desde $136.000.000.  Son los datos publicados en la ficha, así que prefiero no agregar interpretaciones que no estén verificadas. Si quieres, cuéntame qué aspecto te interesa revisar con más detalle y lo vemos punto por punto. | PASS |
| 2 | Mejor sigamos por mensajes. ¿Cuánto bajó el valor vigente? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Claro, seguimos por acá.  La rebaja fue de $8.000.000: pasó de $136.000.000 a los $128.000.000 vigentes, lo que equivale a cerca de un 5,9% menos.  Si quieres, puedo detallarte algún otro dato de la ficha de la propiedad 12001. | PASS |

### synthetic-002 — COLD, UF 2820, property 12002

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | No me interesa visitar por ahora, pero dime cuánto son los gastos comunes. | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Los gastos comunes de ese departamento en Peñalolén son de $80.000 mensuales, según lo indicado en la ficha.  Si más adelante necesitas otro dato de esta propiedad, quedo atenta para ayudarte. | PASS |
| 2 | ¿Y cuántos dormitorios tiene el departamento? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Tiene 3 dormitorios y 3 baños, según lo que indica la ficha de esta propiedad en Peñalolén. La superficie útil es de 47 m² y la total de 52 m².  Si necesitas algún otro dato del departamento, con gusto te lo reviso. | PASS |

### synthetic-003 — HOT, CLP 134000000, property 12003

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Quiero coordinar una visita a la propiedad 12003, idealmente el sábado. ¿Qué sigue? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | agendar_visita | Registré tu preferencia de horario (sabado) para conocer la propiedad 12003. La visita todavía no está confirmada; administración debe validar la disponibilidad y coordinarla contigo. | PASS |
| 2 | ¿Puedo visitar el sábado por la mañana? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | agendar_visita | Registré tu preferencia de horario (sabado por la manana) para conocer la propiedad 12003. La visita todavía no está confirmada; administración debe validar la disponibilidad y coordinarla contigo. | PASS |

### synthetic-004 — NORMAL, UF 2990, property 12004

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Me gustaría que me llame un ejecutivo para conversar sobre la propiedad 12004. | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | contacto_directo | Claro que sí. La propiedad 12004 es un departamento en venta en Macul, de 1 dormitorio y 2 baños, 49 m² útiles, y hoy está en UF 2.990, con la rebaja ya aplicada respecto de su precio anterior. Registré tu solicitud para que converses con un ejecutivo. Si quieres adelantar el trámite, puedes dejarme tu nombre completo y correo; son datos opcionales y también puedes entregarlos directamente al ejecutivo. ¿Qué día o rango horario te acomoda más? | PASS |
| 2 | ¿Podrían escribirme primero por este chat? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | contacto_directo | Te entiendo. Lo que sí queda registrado es tu interés en la propiedad 12004 y tu preferencia de que te escriban. Si me indicas un día o rango horario en que te acomode atender, lo dejo anotado como referencia para esa coordinación. ¿Qué día o rango horario te acomoda más? | PASS |

### synthetic-005 — COLD, CLP 140000000, property 12005

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Quisiera presentar una oferta de $140.000.000 por la propiedad 12005; ¿cómo la canalizo? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | consulta_general | Gracias por la confianza. Te cuento que el precio publicado hoy de la 12005 es justamente $140.000.000 (viene rebajada desde $148.000.000), así que tu oferta calza con el valor de lista, y eso facilita la conversación con el propietario.  La canalización se hace por escrito con el ejecutivo asignado, quien la presenta al dueño: se detallan monto, plazo y forma de pago. Yo registro tu intención de oferta y el ejecutivo te contactará desde su número directo para tomar los antecedentes formales y darle curso; la aceptación finalmente la define el propietario.  ¿La pagarías al contado o con financiamiento? | final_response_semantics:OFFER_HANDLER_ANSWERED |
| 2 | ¿La oferta debe incluir condiciones de pago? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Sí, conviene que la oferta vaya con las condiciones de pago por escrito, porque es lo que el propietario evalúa además del monto. Normalmente se detalla: monto ofertado, forma de pago (contado o con crédito hipotecario), plazo para escriturar y si hay algún anticipo o reserva asociado.  En tu caso el monto ya coincide con el valor de lista de la 12005, así que lo más relevante será definir si pagas al contado o con financiamiento y en qué plazo. Con eso claro, la propuesta queda completa para presentarla al dueño. ¿Cómo la pagarías? | PASS |

### synthetic-006 — HOT, UF 3160, property 12006

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Me puedes mostrar otras propiedades en La Florida con un precio parecido? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Precio: UF 3.160 (referencia $123.240.000) | final_response_semantics:ALTERNATIVE_REQUEST_ANSWERED, final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |
| 2 | ¿Podrías buscar opciones en Macul? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | No encontré coincidencias exactas con esos criterios. Si quieres, podemos ampliar un poco la búsqueda. | final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |

### synthetic-007 — NORMAL, CLP 146000000, property 12007

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | No me interesa esta propiedad; ¿pueden buscar opciones similares en La Reina? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | No encontré coincidencias exactas con esos criterios. Si quieres, podemos ampliar un poco la búsqueda. | final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |
| 2 | Sí, busco algo por Las Condes. | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Gracias, sigo atento a tu consulta. | final_response_semantics:ALTERNATIVE_REQUEST_ANSWERED, final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |

### synthetic-008 — COLD, UF 3330, property 12008

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Cómo queda el valor de la propiedad 12008 después de la rebaja? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Precio: UF 3.330 (referencia $129.870.000) | PASS |
| 2 | No quiero recibir más información, por favor. | BAJA / BAJA | consulta_general |  | PASS |

### synthetic-009 — HOT, CLP 152000000, property 12009

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Qué ventajas tiene la rebaja actual de la vivienda? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Precio: $152.000.000 | PASS |
| 2 | Encontré una vivienda en otra parte y terminé mi búsqueda, gracias. | BAJA / BAJA | consulta_general |  | PASS |

### synthetic-010 — NORMAL, UF 3500, property 12010

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Cuál es el valor vigente de la propiedad 12010 ahora? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Claro, el valor vigente de la propiedad 12010 es UF 3.500, con una rebaja desde las UF 3.600 anteriores. Es un departamento en venta en Ñuñoa, de 3 dormitorios y 2 baños, con 55 m² útiles y gastos comunes de $80.000. Si quieres, te ayudo a revisar cualquier otro detalle de la ficha. | PASS |
| 2 | ¿Cuántos dormitorios y baños informa Prop360? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | La ficha de la propiedad 12010 informa 3 dormitorios y 2 baños, según los datos cotejados con Prop360. Si quieres, puedo revisar contigo algún otro detalle de la ficha. | PASS |

### synthetic-011 — COLD, CLP 158000000, property 12011

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Me interesa el código 12011; prefiero que no me contacten por teléfono. | INTERES_DECLARADO / INTERES_DECLARADO | consulta_general | Gracias por confirmarlo. Queda registrada tu preferencia: seguimos por este canal escrito, sin llamadas telefónicas.  Sobre la propiedad 12011 en Providencia, el precio vigente es $158.000.000, con la rebaja ya aplicada desde los $166.000.000. Es una casa de 4 dormitorios y 3 baños, con 56 m² útiles y 61 m² totales, 1 estacionamiento y gastos comunes de $85.000. La ficha la marca como activa, y de todos modos conviene verificar disponibilidad antes de cualquier reserva.  Si quieres, te cuento más detalles de la ficha o resuelvo alguna duda puntual que tengas. | PASS |
| 2 | ¿Qué gastos comunes están informados en la publicación? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | La publicación informa gastos comunes de $85.000. Ese es el único monto registrado en la ficha para esa propiedad, así que no tengo detalle de qué incluye ni cómo se desglosa. Si te sirve, puedo revisar si hay algún antecedente adicional en la ficha. | PASS |

### synthetic-012 — HOT, UF 3670, property 12012

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | No quisiera coordinar una visita todavía; ¿cuántos dormitorios tiene? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | El departamento del código 12012 tiene 1 dormitorio y 1 baño, con 57 m² útiles (62 m² totales). Quedó con la rebaja aplicada, en UF 3.670.  Cualquier otro dato de la ficha que quieras revisar, te lo comparto sin problema. | PASS |
| 2 | ¿La propiedad sigue activa según Prop360? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Según nuestra ficha interna, el departamento 12012 figura como activo en el portafolio, y el precio de UF 3.670 fue cotejado con Prop360. Lo que sí te aclaro con transparencia: la ficha indica que la disponibilidad debe confirmarse antes de una reserva, así que no puedo darte por cerrado que siga disponible en este minuto.  Si quieres, gestiono la verificación de disponibilidad con el área correspondiente y te confirmo el estado actualizado. Tú me dices. | PASS |

### synthetic-013 — NORMAL, CLP 125000000, property 12013

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Puedo visitar la propiedad 12013 este fin de semana? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | agendar_visita | Claro, registré tu preferencia para visitar la casa 12013 este fin de semana. Te comento con transparencia: la ficha figura activa, pero la disponibilidad exacta se confirma antes de reservar, así que el ejecutivo asignado te contactará desde su número directo para coordinar el día y la hora que calcen.  Sobre la rebaja, el precio actual es $125.000.000 (antes $133.000.000), con 2 dormitorios, 2 baños, 58 m² útiles y 1 estacionamiento. Mientras se coordina la visita, ¿quieres que te adelante algún detalle puntual, como los gastos comunes de $95.000 o la orientación sur?  Si quieres, puedo dejar adelantados tus datos para que el ejecutivo encargado pueda coordinar la visita más rápido. Es opcional; si prefieres, puedes entregárselos directamente. | PASS |
| 2 | ¿Qué día podría ir a verla? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | agendar_visita | Te respondo con honestidad: no puedo confirmarte un día específico, porque la disponibilidad de la casa 12013 se verifica antes de reservar y la agenda la cierra el ejecutivo asignado. Lo que sí puedo hacer es dejar registrada tu preferencia para que esa coordinación sea más rápida. ¿Qué día o rango horario te acomoda más, por ejemplo sábado en la mañana o domingo en la tarde? | PASS |

### synthetic-014 — COLD, UF 2735, property 12014

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Puede contactarme un asesor para hablar de esta propiedad? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | contacto_directo | Claro que sí. Te confirmo que la propiedad 12014 sigue en venta en Macul, con el precio rebajado a UF 2.735 (antes estaba en UF 2.835), 3 dormitorios, 3 baños y 59 m² útiles. Dejo registrada tu solicitud para conversar con un asesor por esta propiedad. Quedo atenta si quieres que le adelante algún detalle específico. ¿Qué día o rango horario te acomoda más? | PASS |
| 2 | También necesito conversar con una persona del equipo. | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | contacto_directo | Por supuesto, entiendo que quieras hablar directamente con una persona del equipo. Dejo registrada tu solicitud de contacto para la propiedad 12014, en venta en Macul a UF 2.735.  Ten claro que no puedo confirmar una llamada ni un horario exacto desde acá, pero el equipo de Procasa se comunicará contigo desde su número directo para conversar sobre esta propiedad. Si quieres, puedo ir dejando anotado algún punto en particular que te interese revisar con el asesor. | PASS |

### synthetic-015 — HOT, CLP 131000000, property 12015

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Quiero ofrecer $131.000.000 por el departamento. ¿Cuál es el siguiente paso? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | consulta_general | Gracias por escribirme. Un dato importante: la propiedad 12015 está publicada como casa en Las Condes, no como departamento, y su precio actual ya es de $131.000.000, justo la cifra que quieres ofrecer. Es decir, tu oferta coincide con el valor de publicación, lo que simplifica bastante la conversación.  El siguiente paso es formalizar esa oferta por escrito, señalando monto, forma de pago y plazos, para que el equipo comercial la revise junto con los antecedentes de la propiedad y te indique cómo presentarla. Puedo dejar registrada tu intención con esos datos. ¿Quieres que la deje anotada así? | final_response_semantics:OFFER_HANDLER_ANSWERED |
| 2 | ¿El propietario revisa una propuesta formal? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Te soy honesta: no tengo acceso a la disposición del propietario ni a sus criterios de decisión, así que no puedo asegurarte que acepte o revise una oferta determinada. Lo que sí puedo decirte es que una propuesta formal, presentada por escrito con monto, forma de pago y plazos, es la vía correcta para que llegue al propietario y la evalúe junto con los antecedentes de la propiedad.  Considerando que el precio de publicación es $131.000.000, tu oferta está alineada con el valor actual. ¿Te gustaría que deje registrada tu intención de presentar la propuesta formal con esos términos? | PASS |

### synthetic-016 — NORMAL, UF 2905, property 12016

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Podrías buscar opciones en La Florida? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | No encontré coincidencias exactas con esos criterios. Si quieres, podemos ampliar un poco la búsqueda. | final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |
| 2 | También me sirven otras alternativas por Macul. | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Gracias, sigo atento a tu consulta. | final_response_semantics:ALTERNATIVE_REQUEST_ANSWERED, final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |

### synthetic-017 — COLD, CLP 137000000, property 12017

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Descarto el código 12017, pero quisiera ver otras propiedades en La Reina. | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | No encontré coincidencias exactas con esos criterios. Si quieres, podemos ampliar un poco la búsqueda. | final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |
| 2 | ¿Tienen otras propiedades en Las Condes? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | No encontré coincidencias exactas con esos criterios. Si quieres, podemos ampliar un poco la búsqueda. | final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |

### synthetic-018 — HOT, UF 3075, property 12018

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Qué precio vigente tiene la propiedad que consulté? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | El precio vigente del departamento 12018 en San Miguel es de UF 3.075 (referencia $119.925.000), con gastos comunes de $80.000. Esa es la rebaja desde las UF 3.175 que tenía antes, así que la diferencia es de UF 100.  Si más adelante quieres que revisemos algún otro antecedente de la ficha, quedo atenta. | PASS |
| 2 | No quiero que me sigan escribiendo ni enviando ofertas. | BAJA / BAJA | consulta_general |  | PASS |

### synthetic-019 — NORMAL, CLP 143000000, property 12019

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Cuál es el valor actual del código 12019? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Claro, el valor actual de la propiedad 12019 es $143.000.000. Esa es la rebaja respecto a los $151.000.000 anteriores, así que hoy queda con ese precio vigente en la ficha.  Si te sirve, puedo comentarte otros datos de la propiedad o revisar alguna alternativa dentro del mismo rango. | PASS |
| 2 | Ya conseguí una casa y mi búsqueda quedó resuelta. | BAJA / BAJA | consulta_general |  | PASS |

### synthetic-020 — COLD, UF 3245, property 12020

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Vi que rebajó la propiedad 12020; ¿qué precio tiene actualmente? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Precio: UF 3.245 (referencia $126.555.000) | PASS |
| 2 | ¿Qué comuna y superficie útil están registradas? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Está registrada en Ñuñoa, Región Metropolitana, y la superficie útil es de 65 m² (la superficie total figura en 70 m²). Cualquier otra duda que tengas, me dices y la reviso en la ficha. | PASS |

### synthetic-021 — HOT, CLP 149000000, property 12021

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Sigo interesada, aunque no acepto llamadas; podemos continuar por aquí. | INTERES_DECLARADO / INTERES_DECLARADO | consulta_general | Por supuesto, seguimos por aquí y sin llamadas de mi parte. Sobre la casa código 12021 en Providencia, el precio vigente es $149.000.000, ya con la rebaja aplicada desde los $157.000.000. Está publicada en venta con 2 dormitorios, 1 baño, 66 m² útiles, 71 m² totales y 1 estacionamiento; los gastos comunes son de $95.000 y la orientación es sur. La ficha aparece como activa. Si quieres, puedo confirmar algún dato puntual o revisar otra propiedad del mismo rango. ¿Hay algo específico que te gustaría aclarar? | PASS |
| 2 | ¿La ficha señala cuántos estacionamientos incluye? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Sí, la ficha indica 1 estacionamiento. Ese es el dato publicado para la propiedad 12021, junto con los 2 dormitorios, 1 baño y 66 m² útiles. Si quieres, puedo revisar otro dato puntual de la ficha. | PASS |

### synthetic-022 — NORMAL, UF 3415, property 12022

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | No quiero ir a verla por el momento, aunque sí necesito saber la orientación. | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Claro, te cuento: según la ficha, el departamento 12022 tiene orientación **oriente**. Ese es el dato publicado; no podría adelantarte más sobre cómo se comporta la luz o el sol en el interior, porque no está en la información oficial.  Te recuerdo que el precio vigente es UF 3.415 (la rebaja desde UF 3.515), con 3 dormitorios, 2 baños y 67 m² útiles en Peñalolén. Si más adelante quieres que revise algún otro detalle de la ficha, quedo atenta. | PASS |
| 2 | ¿La ficha informa estacionamiento y bodega? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Estacionamientos: 0 Bodegas: la ficha no informa este dato; hay que confirmarlo. | PASS |

### synthetic-023 — COLD, CLP 155000000, property 12023

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Me gustaría ir a conocer el departamento; ¿cómo coordinamos una visita? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | agendar_visita | Con gusto te ayudo a coordinar. Un detalle: la propiedad 12023 está registrada como casa en Vitacura (4 dormitorios, 3 baños, 68 m² útiles, 1 estacionamiento), por si la tenías como departamento. El precio vigente es $155.000.000, con la rebaja desde $163.000.000 ya aplicada.  Para avanzar, ¿qué día o rango horario te acomoda? Con tu preferencia, el ejecutivo a cargo confirma la disponibilidad y te contacta desde su número directo para cerrar el horario exacto. La visita queda sujeta a esa confirmación.  Si quieres, puedo dejar adelantados tus datos para que el ejecutivo encargado pueda coordinar la visita más rápido. Es opcional; si prefieres, puedes entregárselos directamente. | PASS |
| 2 | Me acomoda conocerla el domingo, ¿pueden revisar disponibilidad? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | agendar_visita | Perfecto, dejé registrada tu preferencia para el domingo. Te soy honesta: la disponibilidad de ese día aún no está confirmada, así que hay que validarla antes de dar por cerrada la visita.  Para que el ejecutivo pueda orientarte mejor cuando se coordine, ¿cuentas con un crédito preaprobado, comprarías al contado o necesitas asesoría de financiamiento?  Registré tu preferencia de horario (domingo) para conocer la propiedad 12023. La visita todavía no está confirmada; administración debe validar la disponibilidad y coordinarla contigo. | PASS |

### synthetic-024 — HOT, UF 3585, property 12024

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Quisiera hablar con una ejecutiva sobre las condiciones de venta. | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | contacto_directo | Perfecto, con gusto te conecto con una ejecutiva para revisar las condiciones de venta del departamento 12024 en Macul, que hoy está en UF 3.585 tras la rebaja desde UF 3.685 (referencia $139.815.000), con gastos comunes de $70.000. Ya quedó registrada tu solicitud. Si más adelante quieres verlo en persona, me dices y dejamos anotada tu preferencia de día u horario. ¿Qué día o rango horario te acomoda más? | PASS |
| 2 | ¿Me pueden contactar para revisar los antecedentes? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | contacto_directo | Claro que sí. Dejo registrada tu solicitud para que revisen los antecedentes del departamento 12024 en Macul, con el precio actual de UF 3.585 tras la rebaja. Si quieres adelantar el proceso, puedes compartirme de forma opcional tu nombre completo, RUT y correo; si prefieres, los entregas directamente en esa conversación. La disponibilidad actual está activa según ficha, pero siempre se confirma antes de cualquier reserva. ¿Qué día o rango horario te acomoda más? | PASS |

### synthetic-025 — NORMAL, CLP 161000000, property 12025

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Me interesa hacer una oferta por esta vivienda, ¿con quién la reviso? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | contacto_directo | Una posible negociación debe evaluarla administración con el propietario; no puedo confirmar descuentos adicionales. | final_response_semantics:OFFER_HANDLER_ANSWERED |
| 2 | ¿Qué antecedentes necesito revisar sobre el financiamiento? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Para una compra de $161.000.000 conviene tener a mano la preaprobación o simulación hipotecaria del banco, con plazo, tasa y dividendo estimado. Suma el monto disponible para el pie y los gastos operacionales de escrituración, notaría y conservador, y revisa tu capacidad de endeudamiento según renta líquida.  También ayuda saber qué porcentaje del precio cubre el crédito y si la tasación del banco acompaña el valor de compra, porque eso respalda una oferta. Esos antecedentes los puede revisar un asesor contigo y presentarlos ordenados a la administración. | PASS |

### synthetic-026 — COLD, UF 2650, property 12026

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Pueden buscar propiedades en otra comuna, por ejemplo La Florida? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Comuna: La Florida | final_response_semantics:ALTERNATIVE_REQUEST_ANSWERED |
| 2 | ¿Hay otra propiedad disponible en Macul? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Disponibilidad: Activa según ficha; confirmar antes de reservar | final_response_semantics:ALTERNATIVE_REQUEST_ANSWERED, final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |

### synthetic-027 — HOT, CLP 128000000, property 12027

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Esta vivienda no me acomoda; ¿me muestran alternativas por La Reina? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | No encontré coincidencias exactas con esos criterios. Si quieres, podemos ampliar un poco la búsqueda. | final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |
| 2 | Me gustaría ver otra comuna, como Las Condes. | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Comuna: La Reina | final_response_semantics:ALTERNATIVE_REQUEST_ANSWERED, final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED, final_response_semantics:NO_ORIGINAL_COMMUNE_SUBSTITUTION |

### synthetic-028 — NORMAL, UF 2820, property 12028

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Cuánto bajó el valor publicado? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Bajó UF 100: estaba publicada en UF 2.920 y ahora figura en UF 2.820, que es el valor vigente en la ficha de la propiedad 12028, un departamento en venta en San Miguel. Es decir, la rebaja equivale a un 3,4% respecto del precio anterior.  Si quieres, te confirmo cualquier otro dato publicado de esa propiedad, como gastos comunes o metraje. | PASS |
| 2 | Por favor, dejen de contactarme; no deseo más mensajes. | BAJA / BAJA | consulta_general |  | PASS |

### synthetic-029 — COLD, CLP 134000000, property 12029

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Qué aspectos de la ficha debería revisar antes de decidir? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Propiedad 12029 · Casa · Venta Comuna: Santiago (Metropolitana) Precio: $134.000.000 Gastos comunes: $95.000 Dormitorios: 2 Baños: 3 Superficie útil: 74 m² Superficie total: 79 m² Estacionamientos: 1 Orientación: Sur Disponibilidad: Activa según ficha; confirmar antes de reservar | PASS |
| 2 | Encontré un departamento y ya no necesito seguir buscando. | BAJA / BAJA | consulta_general |  | PASS |

### synthetic-030 — HOT, UF 2990, property 12030

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Cuánto bajó la vivienda 12030 y cuál es su precio actual? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Claro, la rebaja fue de UF 100. La vivienda 12030 pasó de UF 3.090 a UF 2.990, y ese es su precio actual, equivalente a una referencia de $116.610.000.  Es un departamento en Ñuñoa, en venta, con 3 dormitorios, 1 baño y 75 m² útiles (80 m² totales), con orientación oriente. Si quieres, puedo revisar algún otro dato de la ficha que te sirva para decidir. | PASS |
| 2 | ¿Qué orientación tiene y sigue activa según la ficha? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Tiene orientación oriente. Y sí, según la ficha figura como activa, aunque la disponibilidad conviene confirmarla antes de una reserva. Si más adelante quieres que la revisemos en persona, me dices y coordino la gestión con el ejecutivo. | PASS |

### synthetic-031 — NORMAL, CLP 140000000, property 12031

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Sí, me interesa, pero por favor no me llamen para nada. | INTERES_DECLARADO / INTERES_DECLARADO | consulta_general | Queda claro, Jorge Pablo: no vamos a llamarte. La atención sigue por escrito, por acá, sin llamadas.  Sobre la propiedad 12031, el precio vigente es $140.000.000, con una rebaja desde los $148.000.000. Es una casa en Providencia, de 4 dormitorios y 2 baños, con 76 m² útiles y 81 m² totales, además de 1 estacionamiento. Los gastos comunes son de $105.000 y la orientación es poniente.  Cuando quieras te detallo algún punto en particular de la ficha, y si en algún momento te interesa conocerla, me lo dices y registro tu preferencia para que el ejecutivo coordine la visita directamente. | PASS |
| 2 | ¿Cuál es la comuna y el metraje útil publicado? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | La comuna es Providencia, en la Región Metropolitana, y la superficie útil publicada es de 76 m². La ficha también indica 81 m² totales.  Cualquier otro dato que quieras revisar, me dices y lo vemos por acá. | PASS |

### synthetic-032 — COLD, UF 3160, property 12032

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | No estoy interesada en visitar, pero quisiera conocer el precio publicado. | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Claro, el precio publicado actualmente es UF 3.160 (referencia $123.240.000). Esa es la rebaja vigente respecto de los UF 3.260 anteriores, así que el valor en la ficha ya está actualizado.  Si más adelante quieres que revise algún otro dato de la propiedad, quedo atenta. | PASS |
| 2 | ¿Qué gastos comunes registra la publicación? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | La publicación registra gastos comunes de $70.000 mensuales. Es el dato que aparece en la ficha de la propiedad 12032, así que ese es el monto informado.  Si necesitas revisar algún otro antecedente de la ficha, quedo atenta. | PASS |

### synthetic-033 — HOT, CLP 146000000, property 12033

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Quisiera visitar esta propiedad el viernes, si existe disponibilidad. | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | agendar_visita | Claro, quedamos atentos. Registré tu preferencia para visitar la casa 12033 en Vitacura un viernes. Eso sí, te soy honesta: no puedo confirmarte el horario desde aquí, porque la disponibilidad la valida el ejecutivo a cargo antes de agendar. La propiedad figura activa en la ficha, con 2 dormitorios, 1 baño, 78 m² útiles y 1 estacionamiento, a $146.000.000 (rebajada desde $154.000.000).  Para que el ejecutivo pueda orientarte mejor cuando tome tu caso, ¿cuentas con un crédito preaprobado, comprarías al contado o necesitas asesoría de financiamiento?  Si quieres, puedo dejar adelantados tus datos para que el ejecutivo encargado pueda coordinar la visita más rápido. Es opcional; si prefieres, puedes entregárselos directamente.  Registré tu preferencia de horario (viernes) para conocer la propiedad 12033. La visita todavía no está confirmada; administración debe validar la disponibilidad y coordinarla contigo. | PASS |
| 2 | Sí, quisiera coordinar la visita para el viernes. | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | agendar_visita | Registré tu preferencia de horario (viernes) para conocer la propiedad 12033. La visita todavía no está confirmada; administración debe validar la disponibilidad y coordinarla contigo. | PASS |

### synthetic-034 — NORMAL, UF 3330, property 12034

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Por favor, que me contacte un asesor para resolver unas dudas. | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | contacto_directo | Claro que sí, con gusto. Sobre el departamento 12034 en Macul, el precio vigente es UF 3.330 (rebajado desde UF 3.430), con 3 dormitorios, 2 baños y 79 m² útiles. Si quieres, puedo dejar registrada tu consulta específica para que llegue con el contexto y te responda más rápido. ¿Qué te gustaría aclarar primero: precio, gastos comunes o características del departamento? | PASS |
| 2 | Prefiero recibir la información por escrito antes de hablar. | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Propiedad 12034 · Departamento · Venta Comuna: Macul (Metropolitana) Precio: UF 3.330 (referencia $129.870.000) Gastos comunes: $80.000 Dormitorios: 3 Baños: 2 Superficie útil: 79 m² Superficie total: 84 m² Estacionamientos: 0 Orientación: Oriente Disponibilidad: Activa según ficha; confirmar antes de reservar | PASS |

### synthetic-035 — COLD, CLP 152000000, property 12035

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Puedo ofertar $152.000.000 por el código 12035? | PRIORIDAD_COMERCIAL / PRIORIDAD_COMERCIAL | consulta_general | Claro, entiendo tu consulta. Te comento que el valor publicado hoy para el código 12035 es de $152.000.000, justamente el monto que mencionas: es la rebaja vigente desde los $160.000.000 anteriores.  Sobre presentar una oferta formal por ese monto, no soy quién para confirmarla ni aceptarla por acá. Eso lo evalúa el ejecutivo junto al propietario, considerando las condiciones de la operación. Si quieres, puedo dejar registrado tu interés en ofertar por $152.000.000 y él lo revisa directamente contigo.  ¿Te gustaría que quede plasmada esa intención de oferta? | PASS |
| 2 | ¿Cómo se presentan los antecedentes de la oferta? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Buena pregunta. Los antecedentes formales de una oferta los define y solicita el ejecutivo a cargo, ya que dependen del tipo de operación y de las condiciones que pida el propietario: no manejo por acá el detalle ni el listado exacto de documentos.  Lo que sí puedo hacer es dejar registrada tu intención de ofertar $152.000.000 por el código 12035, para que él te explique directamente qué antecedentes se presentan y en qué orden. ¿Quieres que quede así anotado? | PASS |

### synthetic-036 — HOT, UF 3500, property 12036

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | Busco un departamento en La Florida; ¿tienen algo similar? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | No encontré coincidencias exactas con esos criterios. Si quieres, podemos ampliar un poco la búsqueda. | final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |
| 2 | Estoy buscando algo por Macul. | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | Gracias, sigo atento a tu consulta. | final_response_semantics:ALTERNATIVE_REQUEST_ANSWERED, final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |

### synthetic-037 — NORMAL, CLP 158000000, property 12037

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | No quiero esta propiedad. ¿Podrían recomendarme otra en La Reina? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | No encontré coincidencias exactas con esos criterios. Si quieres, podemos ampliar un poco la búsqueda. | final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |
| 2 | ¿Pueden enviarme alternativas en Las Condes? | BUSCA_ALTERNATIVAS / BUSCA_ALTERNATIVAS | consulta_general | No encontré coincidencias exactas con esos criterios. Si quieres, podemos ampliar un poco la búsqueda. | final_response_semantics:ALTERNATIVE_TARGETS_ADDRESSED |

### synthetic-038 — COLD, UF 3670, property 12038

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Qué datos principales tiene la vivienda rebajada? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | Claro, es el departamento en San Miguel, código 12038, que quedó rebajado de UF 3.770 a UF 3.670 (referencia $143.130.000).  Tiene 3 dormitorios y 3 baños, con 83 m² útiles y 88 m² totales, orientación oriente y gastos comunes de $100.000. En la ficha aparece sin estacionamientos, y la disponibilidad está activa según los datos publicados, así que conviene confirmarla al momento de avanzar.  ¿Quieres que te amplíe algún punto en particular, como el detalle de los baños o los gastos comunes? | PASS |
| 2 | No kiero resivir mas informacion, porfa. | BAJA / BAJA | consulta_general |  | PASS |

### synthetic-039 — HOT, CLP 125000000, property 12039

| Turn | Input | Expected / obtained | Core intent | Reply | Checks |
|---:|---|---|---|---|---|
| 1 | ¿Qué precio aparece publicado para la propiedad? | CONSULTA_INFORMATIVA / CONSULTA_INFORMATIVA | consulta_general | El precio publicado para la propiedad 12039 es $125.000.000. Esa es la rebaja vigente, desde los $133.000.000 anteriores, y corresponde al valor que figura hoy en la ficha del aviso.  Si quieres, te puedo comentar los demás datos publicados de esta casa en Santiago, como dormitorios, superficies o gastos comunes. | PASS |
| 2 | Por suerte encontré una propiedad, ya cerré la búsqueda. | BAJA / BAJA | consulta_general |  | PASS |
