# ESTADO CONSOLIDADO — PREPARACIÓN PARA PILOTO DE UN TELÉFONO

Este bloque tiene prioridad sobre las secciones históricas de avances previos
que aparecen más abajo. Todas las funcionalidades siguen desactivadas y sin
desplegar; la PR #168 permanece en borrador.

Código añadido:
- executive_whatsapp_standalone.py: aplicación FastAPI aislada. No toca webhook.py.
  Tiene un canary limitado a UNA sesión, doble autorización y privacidad.
- chatbot/executive_whatsapp_gateway.py: receptor autenticado, body acotado,
  escritura confirmada antes de ACK y sin clientes de envío.
- chatbot/executive_whatsapp_audit.py: identidad conservadora, timestamps
  originales o aproximados explícitos, idempotencia y control BSON.
- chatbot/executive_whatsapp_cohort.py: consulta agregada 30/60 días.
- chatbot/executive_whatsapp_export.py: parser TXT con checksum.
- chatbot/executive_whatsapp_backfill.py: importación manual con dos revisores
  y control de asignación; escribe solo mensajes del lead.
- chatbot/executive_whatsapp_audit_runner.py: minimización parcial de datos.
- chatbot/executive_whatsapp_deepseek.py: transporte JSON actualizado,
  bloqueado por autorización explícita de privacidad.
- chatbot/executive_whatsapp_audit_orchestrator.py: análisis supervisado,
  fuera de webhook; nunca modifica SLA, chatbot ni campañas.
- chatbot/executive_whatsapp_audit_storage.py: guarda resultado preliminar
  de auditoría dentro del documento de leads.
- chatbot/executive_whatsapp_semantic.py: genera texto de preferencias
  revisadas sin crear embeddings ni enviar mensajes.

**No existe todavía una vista CRM de conversaciones, un proceso automático de
análisis ni un índice vectorial operativo.** Estos elementos no son necesarios
para demostrar captura y atribución con un teléfono, pero sí para el sistema
comercial completo.

La entrega no autoriza conectar seis teléfonos. Para un teléfono real exige:
revisión de privacidad, aprobación de costos de un servicio separado,
CI del SHA exacto, credenciales de mínimo privilegio, activación explícita
de la app aislada y pruebas canarias de recepción y recuperación histórica.
Consulta el procedimiento:
docs/executive_whatsapp_one_phone_canary_runbook.md

No se garantiza reintento por HTTP 503 de WaSender: antes del piloto se debe
verificar respuesta real y alternativa de reconciliación. Los tests con mocks
no prueban este comportamiento del proveedor.

---

# PROCASA — Auditoría WhatsApp de ejecutivos (precontrato, rama aislada)

**Estado: prototipo no desplegado**, sin endpoints expuestos ni sesiones de ejecutivos activadas.

## Alcance

- El chatbot **únicamente** opera en el WhatsApp de oficina.
- Los WhatsApp de seis ejecutivos sólo capturan y auditan gestiones autorizadas, sin respuestas automáticas.
- Primera cohorte: leads **asignados** en los últimos 30 días, ampliable a 60 días si se demuestra cobertura histórica.
- Sin campañas, reasignación, cambios de SLA ni modificaciones de producción en la prueba.
- Una consulta pasada **no constituye consentimiento automático** para campañas.

## Componentes disponibles en esta rama

1. chatbot/executive_whatsapp_audit.py: normalizador del evento messages.upsert (objeto o lista); validación X-Webhook-Signature por secreto de sesión; diferenciación de fromMe; verificación de teléfono PN/LID; rechazo de grupos, contactos ajenos al CRM y ciclos ambiguos; atribución por leads y crm_assignment_cycles; idempotencia; ingest_upsert con commit=False por defecto; almacenamiento opt-in en el propio documento de leads; audit_transcript de lectura.
2. chatbot/executive_whatsapp_classifier.py: contrato JSON para DeepSeek, estado comercial, evidencia verificable, cobertura declarada y revisión humana obligatoria; campaña nunca habilitada automáticamente.
3. Pruebas unitarias offline: mensajes de cliente y ejecutivo, duplicados, exclusiones, reasignaciones y evidencia de IA.

## Almacenamiento

El canal de oficina mantiene leads.messages intacto, con roles user/assistant.
El canal de ejecutivos se guarda, si se activa expresamente, en leads.executive_whatsapp_messages con provider_key, provider_message_id, actor_type, executive_user_id, assignment_cycle_id, session_id, content, message_type, timestamp, timestamp_origin, ingestion_source y channel.

La vista cronológica une ambos arrays al consultar, pero no mezcla los roles ni altera el mecanismo actual del chatbot.

## Privacidad y seguridad

- La sesión y el ejecutivo se determinan desde configuración del servidor, **nunca** desde los datos del webhook.
- Un webhook independiente y un secreto por sesión; no reutilizar la ruta del chatbot de oficina.
- Guardar sólo mensajes 1:1 con cliente conocido y asignación inequívoca durante la fecha del mensaje.
- Excluir chats personales sin match, grupos, números LID irresolubles y ambigüedades. No crear leads nuevos.
- Sin descarga automática de multimedia ni secretos en el repositorio.
- Diseñar política de acceso/retención. MongoDB limita a 16 MiB el documento; prever archivo y cuotas si crece.

## Pendientes indispensables antes de puesta en marcha

1. Validar con un teléfono real que messages.upsert entregue mensajes escritos manualmente desde móvil, en ambas direcciones.
2. **Confirmar o descartar el historial anterior a la vinculación** (30–60 días). WaSender no garantiza su recuperación íntegra en la documentación pública.
3. Si no hay backfill, implementar importación autorizada de chats exportados, aún no incluida.
4. Diseñar una ruta HTTP autenticada y aislada, con secretos por sesión. Este módulo NO está conectado al servidor.
5. Incorporar cola durable, gestión de reintentos, límites de BSON, pruebas de carrera y acceso restringido desde el CRM.
6. Invocar DeepSeek desde un orquestador explícito. Hoy existe el contrato y su validación, **no clasificación de conversaciones reales**.
7. Ejecutar CI, hacer una prueba con una sola sesión y comparar 15–20 conversaciones reales antes de ampliar el plan.

## Decisión comercial

No ampliar directamente a seis sesiones antes de demostrar la cobertura histórica con una conexión de prueba. Si el historial no es accesible, usar exportación controlada para el piloto y conservar WaSender como captura futura.

## Documentación WaSender

- https://wasenderapi.com/api-docs/webhooks/webhook-message-upsert/
- https://www.wasenderapi.com/api-docs/getting-started/how-to-receive-messages-and-media-from-wasenderapi
- https://app.wasenderapi.com/api-docs/webhooks/webhook-setup/
- https://wasenderapi.com/help/messaging/handling-duplicate-webhooks


## Avance posterior: adaptador HTTP y cohorte (todavía sin activar)

Se incorporaron **dos componentes nuevos no registrados en webhook.py**:

- chatbot/executive_whatsapp_gateway.py:
  - función build_unmounted_executive_audit_router;
  - registro de sesiones por alias opaco y SessionBinding inyectados por configuración confiable;
  - validación de encabezado X-Webhook-Signature frente al secreto de la sesión;
  - tamaño HTTP limitado, rechazo de JSON inválido y claves duplicadas;
  - sólo evento messages.upsert y lotes acotados;
  - dos barreras de activación: enabled=False y persist_enabled=False por defecto;
  - operaciones síncronas de Mongo ejecutadas fuera del event loop;
  - respuesta con conteos sin datos de clientes;
  - ante fallo de almacenamiento responde 503, no declara evento almacenado;
  - NO IMPORTA el bot, endpoints de envío ni workers de campañas;
  - no montado en FastAPI de producción, ningún teléfono conectado.

- chatbot/executive_whatsapp_cohort.py:
  - creación de consulta READ-ONLY para cohortes de 30/60 días por assigned_at de crm_assignment_cycles;
  - reporte agregado por ejecutivo, ciclos y leads sin exponer teléfonos/conversaciones;
  - los conteos no acreditan captura histórica ni entrega del lead.

Las nuevas suites se ejecutan en .github/workflows/executive-whatsapp-audit-ci.yml:
- tests/test_executive_whatsapp_gateway.py
- tests/test_executive_whatsapp_cohort.py
- tests/test_executive_whatsapp_audit.py
- tests/test_executive_whatsapp_classifier.py

### Riesgos que SIGUEN pendientes y prohíben el despliegue

1. **No hay webhook vivo ni cola durable**. La ruta está deliberadamente sin registrar: no debe montarse en producción sin aislamiento, idempotencia bajo concurrencia, reintentos y revisión de privacidad.
2. **No está probada la recuperación histórica** de 30–60 días. No confundir message logs de llamadas API con mensajes manuales antiguos.
3. **No hay importador de chats exportados**, sólo diseño pendiente.
4. **No existe orquestación real con DeepSeek** ni guardado de auditorías; el clasificador es contrato validado, no evaluación en producción.
5. **No se ha validado el límite BSON de 16 MiB bajo concurrencia real**. Bloquear escritura masiva hasta implementar guard y pruebas apropiadas.
6. **No existen todavía permisos de auditoría en UI** ni política de consentimiento/retención acordada.
7. La muestra oficial de Webhook Upsert no garantiza timestamp original de cada mensaje; el normalizador marca timestamp_origin="webhook_timestamp" cuando sólo existe fecha del evento. Esa marca no autoriza inferir gestiones históricas o SLA.

### Fuentes verificadas

- https://wasenderapi.com/api-docs/webhooks/webhook-setup/
- https://wasenderapi.com/api-docs/webhooks/webhook-message-upsert/

**Estado de aceptación para conexión real: NO APTO, PENDIENTE DE VALIDACIONES.**


## Extensión: persistencia protegida, auditoría IA y búsqueda semántica

### Escritura de mensajes

- Claves de evidencia WaSender con SHA-256, sin teléfono literal.
- Si un teléfono corresponde a dos leads, la identidad queda ambigua y NO se escribe.
- Timestamps sin zona horaria: no se interpretan como UTC automáticamente.
- Requisito obligatorio: timestamp original del mensaje para atribución a un ciclo. La fecha del webhook no sirve para datar mensajes históricos.
- Inserción condicionada atómicamente por deduplicación y tamaño BSON del documento actual, con techo conservador de 12 MiB (frente a 16 MiB del servidor).
- Pruebas de concurrencia en Atlas NO ejecutadas: no asumir que mocks las sustituyen.

### Contrato de IA

Archivo: chatbot/executive_whatsapp_audit_runner.py.

- Prepara entrada de sólo lectura. Usa evidencia cronológica y distingue chatbot, cliente y ejecutivo.
- Minimiza algunos PII: URL, email, teléfono móvil chileno e identificadores internos.
- Transport hacia modelo inyectado. NO tiene cliente DeepSeek configurado, llamadas de red, escrituras, ni actos comerciales.
- Rechaza respuesta no JSON, evidencia inventada y contacto efectivo sin ambos participantes en el mismo ciclo.
- Si la transcripción es truncada, nunca afirma que la cobertura es completa.
- Regresa únicamente una propuesta con revisión humana pendiente.
- La depuración por regex NO garantiza anonimización plena: enviar chats reales al proveedor de IA exige autorización y revisión legal.

### Capa de embeddings

Archivo: chatbot/executive_whatsapp_semantic.py.

- NO vectoriza cada mensaje ni conversaciones completas.
- Sólo genera perfiles estructurados con preferencias aprobadas por revisión humana, campos y valores limitados a listas cerradas.
- Excluye automáticamente bajas, operaciones resueltas, falta de evidencia y perfiles sin preferencias comerciales útiles.
- Genera revision SHA-256 para detectar cambios.
- No invoca un modelo de embeddings, no crea índice vectorial y no almacena vectores en MongoDB.
- Cualquier índice futuro debe aplicar controles de acceso, retención, baja y permisos del CRM.

### Pendientes que mantienen bloqueado cualquier despliegue

1. Confirmar retrospectiva WaSender de 30/60 días con un teléfono real y autorización.
2. Implementar cola durable, ACK tras guardar, retry y monitoreo sin PII.
3. Probar limitación BSON y escritura atómica con concurrencia en DB no productiva.
4. Aprobar política de tratamiento, acceso, retención y exclusión de chats personales.
5. Integrar DeepSeek con privacidad aprobada, presupuesto y evaluación de precisión humana.
6. Almacenar resultados aprobados en leads sin tocar SLA ni lógica CRM.
7. Diseñar índice de embeddings con revocación y eliminación de datos.
8. Registrar ruta de webhook protegida mediante activación explícita; actualmente sigue SIN montar.
9. Implementar importador de exportaciones TXT con verificación humana de lead/ciclo; hoy sólo existe un parser.
10. Piloto canario y evaluación de 15-20 conversaciones antes de ampliar a seis sesiones.

No fusionar ni desplegar PR #168. Nunca permitir que el chatbot opere en teléfonos de ejecutivos. Los logs de envíos API no acreditan historial manual antiguo.
