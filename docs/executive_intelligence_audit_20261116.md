# Auditoría de inteligencia comercial ejecutiva — 2026-11-16

**Veredicto: REQUIERE CORRECCIONES. No aprobado para incorporar los otros cinco ejecutivos.**

Base auditada: fafb3770 (PR #216). Cambios aislados en codex/executive-intelligence-audit. No se desplegó, no se modificó MongoDB de producción, no se enviaron mensajes ni campañas y no se consultaron conversaciones privadas. Todas las conversaciones evaluadas son sintéticas.

## Diagnóstico y correcciones

La implementación tenía buenas fronteras iniciales: firma por sesión, comprobación de asignación, inserción atómica, deduplicación, worker separado y flags cerrados por defecto. Sin embargo, las 39 pruebas iniciales no detectaban fallos comerciales y de integración.

| Severidad | Reproducción / causa | Corrección |
|---|---|---|
| P1 | `No quiero agendar una visita` producía ASK_VISIT; `No dije que ya compré una casa` cerraba la búsqueda | Reglas conservadoras por cláusula, negación, citas e hipótesis; abstención |
| P1 | Baja seguida de `Hola` perdía la supresión sugerida | Separación de permiso de contacto, intención y estado; baja persistente incluso ante reactivación |
| P1 | Ejecutivo propone visita y cliente describe búsqueda: prueba antigua aceptaba visita inventada | Modelo queda como sugerencia sin confirmar; evidencia determina salida operativa; prueba corregida |
| P1 | Orden de llegada confundido con respuesta del cliente | Orden temporal, deduplicación y empate temporal explícitamente incierto |
| P1 | Lease vencido permitía publicar resultados de otro trabajador | Token de propietario y CAS de sesión/ciclo/mensaje; revalidación de usuario y captura después del modelo |
| P1 | Cada mensaje de oficina recortaba el historial canónico a 50 | Conservación del historial; límite de 50 aplicado al contexto de oficina |
| P1 | Lectores de campañas/oficina interpretaban mensajes ejecutivos pasivos | Frontera `office_messages` en lectores de políticas, campañas y respuestas |
| P2 | Lecturas completas, últimos ocho mensajes, filas con lease bloqueando la cola | Proyección sin historial al reclamar, filtrado MongoDB autorizado, lotes de 200 con cursor y dos turnos de contexto; índice parcial |
| P2 | JSON débil y confianza arbitraria 0,95 | Validación estricta; confianza no calibrada explícita; timeout 12 s sin reintentos automáticos |

Archivos centrales: executive_conversation_intelligence.py, executive_semantics.py, executive_intelligence_worker.py, storage.py y message_scope.py. No se cambiaron reglas formales de SLA, asignaciones ni temperatura canónica. Se reutiliza HOT_INTENTS y el catálogo de comunas. La clasificación de campañas mantiene su propia semántica histórica; se evita alimentarla accidentalmente con capturas ejecutivas.

El contrato v2 añade necesidades separadas DEMAND/OWNER_SELL/OWNER_RENT, evidencia por campo, permiso de contacto y solicitud importante pendiente. `summary.search` conserva la necesidad de demanda. Los atributos ausentes son desconocidos; no se inventan. Una respuesta del ejecutivo no confirma cumplimiento de la solicitud ni gestión formal CRM. Resúmenes v1 se reconstruyen cuando hay trabajo pendiente; no hay migración automática de producción.

## Evidencia cuantitativa

- Regresión crítica: **1.803 pruebas aprobadas**, 80 archivos, incluyendo campañas y SLA.
- Suite ejecutiva final: **123 aprobadas, 1 omitida**, incluye 15 integraciones con MongoDB 8.0 local real. Estas cifras se superponen; no deben sumarse.
- MongoDB: seis reclamantes concurrentes, deduplicación concurrente, reasignación/revocación durante clasificación, leases, índice, conservación de historial y 450 mensajes en tres lotes.
- Desarrollo sintético: 336 conversaciones, **112 familias con tres variantes correlacionadas**. Macro-F1 reglas iniciales 0,565; finales 1,000. Este conjunto fue usado para desarrollo y NO acredita generalización.
- Formulaciones nuevas congeladas: 70 casos, 14 categorías. Macro-F1 inicial 0,160; salida protegida final **0,304**. Hay 53 abstenciones y tres confusiones no críticas. No alcanza aceptación.
- DeepSeek real, mismo conjunto nuevo: **64/70 intenciones correctas**, macro-F1 **0,912**, una respuesta rechazada por validación. Dos falsos positivos de alto impacto: llamada hipotética y oferta presentada como ejemplo. Se conservan como sugerencias, no como hechos.
- Extracción determinística: 32 casos (16 familias), 72 hechos correctos, 0 falsos positivos, 6 omisiones; precisión 1,000, recall 0,923, F1 0,960. Faltan jardín, sector y presupuesto escrito con palabras. No es una evaluación de extracción del LLM.
- Transiciones, reactivación, atribución y seguimiento: 36 escenarios (12 familias con orden/duplicados), F1 1,000 en cada dimensión evaluada.

Las matrices completas, TP/FP/FN y métricas por categoría están en los JSON de reports/executive_intelligence. Macro-F1 usa las 14 categorías verdaderas; errores de esquema cuentan como omisión de su categoría y aparecen en la matriz, sin inventar una categoría comercial adicional.

Cero falsos positivos críticos observados en la salida protegida NO demuestra 99% de precisión: la cobertura es baja y las muestras positivas pequeñas. Con cero errores se necesitarían aproximadamente 299 predicciones positivas independientes por categoría para un límite inferior unilateral del 99% con confianza del 95%. Las variantes correlacionadas no cuentan como observaciones independientes.

Prueba real final: deepseek-flash, 43.373 tokens de entrada y 10.410 de salida; mediana 1.469 ms, p95 2.328 ms. Hubo además 56 llamadas exploratorias, documentadas separadamente. No se hizo benchmark de proveedores ni estimación de factura con tarifas no verificadas. El worker procesa hasta tres leads y duerme 20 s: falta prueba de carga representativa para seis teléfonos.

## Límites pendientes y validación real

1. Mejorar cobertura lingüística y validar evidencia contextual del modelo antes de promover sus sugerencias. El sistema protegido es conservador y todavía pierde demasiadas intenciones. No ajustar sobre el conjunto congelado y luego llamarlo validación independiente.
2. Validar el circuito real de Pablo con consentimiento y contacto comercial asignado: firma, sesión, mensaje saliente manual, respuesta posterior, recepción fuera de orden, duplicado, resumen y revisión humana. Usar endpoint por sesión; el endpoint piloto antiguo no alimenta esta cola. No se realizó esa prueba ni se inspeccionó configuración de Render.
3. Definir operación humana para restricciones de contacto y revisión: `campaign.enforced=false`; el resumen NO ejecuta bajas ni campañas. No habilitar campañas confiando solamente en este resumen.
4. El filtrado autorizado se ejecuta en Mongo, pero el array embebido todavía requiere recorrido servidor y crecimiento sujeto al límite de documento. Falta política de archivo. No se afirma escalabilidad validada por tener seis reclamantes concurrentes.
5. Redacción de correo/teléfono/RUT/URL reduce PII, pero no anonimiza nombres o direcciones en texto libre. Asignación comercial no prueba que cada frase de un contacto sea comercial. Hace falta validar consentimiento y minimizar contexto antes de habilitar el modelo con datos reales.
6. La prueba de política phase3 se omite porque main ya importa `build_property_search_response`, inexistente en conversation_policy. Se reprodujo contra ambos archivos originales de fafb3770. Es un defecto previo pendiente; no se ocultó como prueba aprobada ni se reconstruyó política ajena a esta auditoría.
7. El proveedor admite tiempo de evento como alternativa al tiempo de mensaje y rechaza mensajes antiguos según su ventana actual. Falta validar con eventos reales la semántica de tiempo/replay antes de evaluar ejecutivos.

## Reproducción

`python scripts/evaluate_executive_intelligence.py`

`python scripts/evaluate_executive_intelligence.py --holdout --output reports/executive_intelligence/holdout.json`

`python scripts/evaluate_executive_dimensions.py`

`python -m pytest tests/test_executive_intelligence_adversarial.py tests/test_executive_office_boundary.py`

Para integración, EXEC_AUDIT_TEST_MONGO_URI debe apuntar exclusivamente a localhost; las pruebas crean y eliminan una base sintética única. CI añade un servicio MongoDB desechable. `--live-model --holdout` ejecuta llamadas reales pagadas usando credenciales del entorno; nunca consulta leads.

Fuentes técnicas contrastadas: [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/), [Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion/), [MongoDB indexOfArray](https://www.mongodb.com/docs/manual/reference/operator/aggregation/indexofarray/). JSON válido no garantiza interpretación correcta.

## Métricas de intención del modelo real (70 casos)

| Categoría | TP | FP | FN | Precisión | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| ASK_ALTERNATIVES | 3 | 0 | 2 | 1.000 | 0.600 | 0.750 |
| ASK_CONTACT | 5 | 1 | 0 | 0.833 | 1.000 | 0.909 |
| ASK_INFO | 5 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| ASK_VISIT | 5 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| GIVE_OFFER | 5 | 1 | 0 | 0.833 | 1.000 | 0.909 |
| INTERESTED | 5 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| MIXED_NEEDS | 5 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| NO_SIGNAL | 2 | 0 | 3 | 1.000 | 0.400 | 0.571 |
| OPT_OUT | 5 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| OWNER_RENT | 5 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| OWNER_SELL | 4 | 0 | 1 | 1.000 | 0.800 | 0.889 |
| PROPERTY_DECLINED | 5 | 2 | 0 | 0.714 | 1.000 | 0.833 |
| SEARCH_COMPLETED | 5 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| SEARCH_UPDATED | 5 | 1 | 0 | 0.833 | 1.000 | 0.909 |
