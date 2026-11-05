# Candidata V14 de conversación para rebajas PROCASA

**Estado:** implementación aislada para revisión. No está aprobada ni habilitada para uso productivo.

## Referencia y alcance

La base reproducible V13.1 es el merge de PR #127, commit `8085a536f1806ea9ef76f3a79c6922cb372889c7` (4 de noviembre de 2026). La candidata parte de `origin/main` en `3cbd52c529b1dc693dbab80133490387e4529172`. V13.1 continúa siendo la ruta predeterminada; V14 solo se selecciona dentro del teléfono de prueba aislado cuando una campaña de prueba tiene versión V14 y conserva el texto exacto que recibió el destinatario.

No se envió ningún WhatsApp. No se contactaron leads ni ejecutivos reales, no se escribieron datos de producción, no se cambiaron variables de entorno y no se desplegó nada. Las pruebas de ruta usan sender y modelo simulados. Los escenarios incluyen datos ficticios.

## Diagnóstico del recorrido anterior

1. **Entrada:** `webhook.py` normaliza teléfono e ID del proveedor y comprueba primero la lista de teléfonos de prueba. Esa rama llama a `process_test_inbound` antes del registro ordinario de eventos, la cola durable, `chatbot.core`, CRM/SLA y `record_inbound`.
2. **Contexto:** `load_test_context` solo consulta la campaña de prueba y la propiedad Prop360. Exige envío `SENT` no vencido, identidad de propiedad coincidente, disponibilidad para venta y precio actual igual al precio enviado. No consulta leads.
3. **Historial:** los turnos se leen de `crm_price_drop_pilot_test_turns_v1`; se limitan al recuperar y antes de construir el contexto del modelo.
4. **Redacción e interceptores:** V13.1 llama primero a `reply_v13` para saludos, respuestas sí/no, interés, visitas, objeciones y preguntas de precio/ubicación. V12 tiene `followup_reply`. Si no responden, `build_test_reply` también intercepta ciertos patrones y usa `_llm_answer` para el resto. El piloto nativo tiene sus propias respuestas factuales y de visita antes de `generar_respuesta_estructurada`.
5. **Seguridad posterior:** V13.1 aplica `safe_outbound_v13` y luego personalización. Algunas afirmaciones inseguras de visita/contacto pueden sustituir la respuesta completa por un texto fijo.
6. **Efectos:** hay un intento de envío por ID de mensaje entrante; un resultado incierto no se reintenta. V11–V13 pueden enviar después avisos simulados al tester. El clasificador de campaña y las actualizaciones de lead están fuera de la rama de prueba.

| Módulo | Responsabilidad relevante |
|---|---|
| `price_drop_pilot_isolation.py` | Aislamiento por teléfono, atribución de campaña/propiedad, precio y atributos verificados, historial e idempotencia; una respuesta de prueba. |
| `price_drop_pilot_guard_v13.py`, `price_drop_pilot_followup_v12.py` | Interpretación determinista y redacción de respuestas. |
| `price_drop_campaign_classifier.py` | Clasifica intención y estado de campaña; no redacta respuestas. |
| `price_drop_campaign_real.py` | Vista previa y controles de envío real: precio, consentimiento, consulta previa, baja/bloqueo, identidad y duplicados; atribución y marcadores de campaña para envíos aceptados. |
| `chatbot/core.py`, `grok_client.py` | Prompt y generador comercial habituales. |
| `scripts/price_drop_pilot_test.py` | Sender de prueba preexistente para una propiedad de muestra y con bandera `--send`; no se ejecutó ni modificó. |

Los controles de consentimiento, baja, identidad, precio, duplicados, aislamiento e idempotencia protegen operaciones. Las respuestas de saludo, interés, visita y objeción de V12/V13 también imponen un guion comercial. V14 omite esas decisiones de redacción solo tras superar la misma frontera de prueba y validación del contexto.

## Arquitectura candidata

**Contexto verificado → generador comercial habitual → validación de seguridad → respuesta.**

`price_drop_pilot_conversation_v14.py` prepara una vista previa pura del mensaje inicial y arma el contexto conversacional para `generar_respuesta_estructurada`. El modelo recibe el texto saliente exacto como turno `assistant`, el mensaje actual, motivo del contacto, propiedad y precios verificados, atributos disponibles, historial reciente, pregunta pendiente, afirmaciones literales del cliente y estados operativos confirmados. No hay interceptores de V14 que redacten saludos, interés o preguntas comerciales.

La candidata valida precios UF, medidas, dirección/unidad, referencia a consulta previa, disposición de negociación del propietario, solicitudes de datos personales y afirmaciones de visitas, contactos o escrituras en CRM. Ante una respuesta parcialmente insegura solicita una sola reparación; si persiste el riesgo, conserva frases seguras completas cuando puede y, si no, usa una respuesta de contingencia. La salida inválida o un timeout no activa reintentos de formato internos. El handler mantiene un máximo de un intento de WhatsApp por mensaje y V14 no envía avisos simulados.

El contexto, respuesta original, salida final, reglas que intervinieron, errores, reparaciones, modelo, intentos al proveedor, latencia y tokens disponibles quedan en el turno aislado. El cliente actual no proporciona costo, por lo que se registra como no disponible.

## Vista previa del mensaje inicial

Ejemplo ficticio: propiedad 6436, departamento de San Miguel, precio anterior UF 2.790 y precio vigente verificado UF 2.690, con evidencia de consulta previa.

> ¡Hola! ¿Cómo estás? 😊 Te escribimos de PROCASA porque hace un tiempo consultaste por el departamento de San Miguel.
>
> Te quería contar que bajó 100 UF: antes estaba en UF 2.790 y ahora en UF 2.690.
>
> Te dejo la publicación actualizada para que puedas revisarla:
> https://www.procasa.cl/6436
>
> ¿Te gustaría volver a mirarlo con este nuevo precio?

La referencia a la consulta anterior se omite si falta evidencia. La segunda propiedad ficticia prueba una casa en La Florida, código 92817 y precios UF 5.600/5.350. La vista previa bloquea precio no verificado, propiedad que no está disponible para venta, precio propuesto distinto del vigente, código inválido o enlace que no corresponda a Procasa y al código.

## Evidencia ejecutada

- V13.1 exacta, batería de piloto y regresión: **409 aprobadas** en el commit de referencia.
- Rama candidata, la misma batería más pruebas V14: **424 aprobadas y 1 omitida** con Python 3.13. La prueba omitida usa un `start_at` fijo (`2026-11-05 14:00 UTC`), que ya había vencido al ejecutar; el chequeo correcto de producción de pruebas rechazó ese horario con `schedule_must_be_future`. La ejecución sin exclusiones tuvo 424 aprobadas y esa única falla temporal.
- Pruebas aisladas de conversación V14 y baja: **14 aprobadas**. El test confirma que «No quiero recibir más mensajes» detiene la campaña de prueba antes del modelo o del sender.
- `py_compile` de los módulos modificados y `git diff --check`: aprobados.
- Pruebas nuevas: vista previa dinámica y controles de verificación; contexto con texto saliente exacto; reparación acotada y conservación de frases seguras; baja antes de generar; ausencia de avisos simulados y escrituras CRM en la ruta; deduplicación; fallo cerrado cuando no existe el texto inicial; fallback por JSON inválido, timeout y salida vacía.

Estos conteos prueban regresiones deterministas y aislamiento; no son puntuaciones de naturalidad ni demuestran mejora frente a V13.1.

### Transcripción representativa de la prueba determinista

Esta transcripción usa respuestas fijas de un stub para comprobar la reparación; **no** es una ejecución con un LLM real.

- **Mensaje inicial enviado en fixture:** el texto de vista previa mostrado arriba.
- **Cliente:** «¿Cuál es el precio?»
- **Respuesta original del stub:** «El precio vigente es UF 2.690. Tu visita ya quedó confirmada.»
- **Reparación única del stub:** «El precio vigente es UF 2.690.»
- **Texto final aceptado por el sender simulado:** «El precio vigente es UF 2.690.»

La respuesta falsa de visita se eliminó y el dato de precio verificado se conservó. El test confirma un único intento de envío y cero avisos simulados.

## Comparación de veinte conversaciones y rúbrica

El archivo `tests/fixtures/price_drop_conversations_v14.json` contiene veinte escenarios conversacionales de varios turnos, derivados de los ejemplos y casos límite de la solicitud: saludo, sí/no contextual, negociación, rechazo de visita, preguntas combinadas, segunda propiedad, errores ortográficos y horarios ambiguos. La solicitud menciona una batería y rúbrica de Astra, pero esos artefactos no estaban adjuntos ni en el repositorio; por eso no presento este fixture como los escenarios originales de Astra.

La comparación completa V13.1 / V14 / generador comercial original con conversaciones completas y modelo real **no se ejecutó**. La configuración señala `deepseek-v4-flash`, pero no hay clave DeepSeek/XAI en el entorno ni en `.env`. No se intentó ninguna llamada, no se copió una clave y no se modificó configuración. Tampoco hay transcripciones reales con las que identificar casos de calidad que mejoraron o empeoraron. No atribuyo a V14 una mejora de naturalidad, continuidad, pertinencia o utilidad comercial.

Por falta de esa evidencia, V13.1 se mantiene como ruta predeterminada y la candidata no debe habilitarse basándose en este borrador. Para reconsiderarla hacen falta la rúbrica y batería originales de Astra, seguida de evaluación aislada con datos ficticios en el generador real; registrar y puntuar las veinte conversaciones completas, incluyendo casos mejores y peores, y comprobar cero fallos críticos.

## Archivos modificados

- `.github/workflows/production-ci.yml`
- `chatbot/grok_client.py`
- `chatbot/price_drop_pilot_conversation_v14.py` (nuevo)
- `chatbot/price_drop_pilot_isolation.py`
- `docs/price_drop_conversation_v14_candidate.md` (este informe)
- `tests/fixtures/price_drop_conversations_v14.json` (nuevo)
- `tests/test_price_drop_pilot_conversation_v14.py` (nuevo)
- `tests/test_price_drop_pilot_isolation.py`

## Riesgos pendientes

Los validadores de salida cubren afirmaciones críticas conocidas, pero no demuestran la seguridad semántica de toda respuesta en lenguaje natural. El cliente comercial actual no expone costos. La revisión humana de conversaciones completas con modelo real, la rúbrica de Astra y la batería original siguen pendientes. No se recomienda activar V14 hasta disponer de esa evidencia y confirmar mejora suficiente sin fallos críticos.

## Revisión técnica posterior

Se corrigieron dos defectos detectados al revisar el código: la reparación ahora recibe la respuesta original y las infracciones detectadas; el presupuesto del historial conserva primero los turnos más recientes y después restituye el orden cronológico. Se agregaron regresiones para ambos casos. La prueba de bootstrap con fecha vencida ahora usa NOW explícito en preview, persistencia y aprobación, sin cambiar el reloj ni los controles de producción.

Verificación posterior: 283 pruebas aprobadas, sin exclusiones, ejecutando test_price_drop_pilot_conversation_v14.py, test_price_drop_pilot_isolation.py y test_price_drop_real_pilot.py con Python 3.13 y UTF-8. Esta verificación reemplaza el resultado con exclusión para la prueba temporal. La evaluación real de conversaciones y la integración de V14 en la ruta de destinatarios reales siguen pendientes; esta revisión no autoriza envíos.
