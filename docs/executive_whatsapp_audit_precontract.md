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
