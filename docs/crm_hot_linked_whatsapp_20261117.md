# Diseño de transición: CRM WhatsApp ejecutivo y HOT sin SLA (17-11-2026)

## Estado de esta rama (fase 1)

Implementa exclusivamente **envío manual desde la ficha del lead en CRM**, con controles de sesión por ejecutivo, confirmación del proveedor y registro en \`leads.messages\`. No activa envíos por defecto, no modifica la temperatura ni la duración del SLA.

- Render: \`PROCASA_EXEC_CRM_DIRECT_SEND_ENABLED=0\` de manera predeterminada. Mantener apagado hasta validar una cuenta conectada y un envío real con consentimiento.
- Render: \`PROCASA_EXEC_CRM_SEND_SESSION_IDS\` lista explícita de IDs autorizados para **enviar**. Es distinta de la allowlist de **captura** \`PROCASA_EXEC_AUDIT_CAPTURE_SESSION_IDS\`; capturar no autoriza enviar.
- La clave \`api_token\` existente en la variable dedicada \`PROCASA_WASENDER_{EJECUTIVO}\` se usa solo en backend, nunca aparece en la página.
- El remitente debe ser el ejecutivo propietario único del ciclo CRM activo y sesión válida. Los administradores no pueden enviar suplantando una cuenta ejecutiva asignada a otra persona.
- Bloquear si el lead es ambiguo, sin teléfono real, con baja explícita o con ciclo de asignación diferente.
- Un ID de intento único se reserva en \`crm_executive_whatsapp_dispatches\` antes del POST al proveedor. Un timeout se registra como incierto y no se reintenta automáticamente.
- Una aceptación con \`provider_message_id\` guarda un mensaje con \`source=crm_manual_api\`, \`channel=executive_whatsapp\`, \`timestamp\` UTC explícito y \`timestamp_source=crm_api_accepted\`, conservando procedencia. La entrega efectiva es un estado posterior.
- El envío manual nunca marca \`first_valid_management_at\`, \`temperature_at_assignment\` ni detiene el SLA por sí mismo.
- La interfaz muestra \`Enviar desde mi WhatsApp\` solo si el servidor confirma capacidad. Para cuentas no autorizadas conserva el comportamiento anterior de \`wa.me\`.

## Fase 2: mensaje automático HOT (aún NO implementada aquí)

**No activar una automatización simplemente porque el campo \`usuarios.wasender.session_id\` existe.** Debe comprobarse que el dispositivo esté conectado, que hay una autorización válida y que no exista STOP o cliente bloqueado.

1. Aplicar solo a asignaciones HOT **nuevas a partir de un cutover explícito**, no a los 24 ciclos HOT históricos de Hernán ni a la cartera actual de otros ejecutivos.
2. Verificar lead HOT real, ejecutivo activo, mismo ciclo, sesión única conectada, remitente correcto, número normalizado y consentimiento verificable del lead para contacto por WhatsApp.
3. Reservar un intento irreversible \`(lead_id, assignment_cycle_id, owner_id, hot_auto_v1)\`; un mismo HOT no puede generar dos saludos automáticos por reintentos/restarts.
4. Usar el token de **esa sesión ejecutiva**, jamás el token del chatbot de oficina. Guardar la respuesta y el resultado en un outbox durable y en el documento canónico.
5. Solo si el proveedor acepta con ID identificable **y se conserva evidencia durable del envío**, registrar una excepción del SLA en ese ciclo, con \`kind=HOT_EXEC_API_CONFIRMED\`, ejecutivo, sesión, mensaje, y sello UTC. Si el proveedor falla o no podemos verificar sesión/consentimiento, mantener el SLA HOT actual.
6. Integrar la excepción en **todas** las vías relevantes: \`calculate_cycle_sla\`, evaluación y envío de alertas, reevaluación antes de reasignar, validaciones transaccionales de la reasignación, API del CRM, paneles e informes. Un flag aislado no basta.
7. Registrar estados de envío \`reserved/provider_attempt/accepted/confirmed/unknown/failed\`; no confundir aceptación por HTTP con recepción o lectura. La IA comercial no genera ni acredita una gestión humana.
8. No alterar nada para leads COLD/NORMAL o para agentes sin sesión vinculada.

## Horas y normalización

El chatbot actual conserva mensajes con zona \`America/Santiago\` (p.ej. \`-03:00\`). El webhook ejecutivo conserva hora UTC \`+00:00\`. Ambos son válidos si se interpretan como instantes. Los nuevos envíos manuales API se registran en UTC con \`timestamp_source=crm_api_accepted\`; el frontend debe mostrar \`America/Santiago\` al usuario. No reconvertir en bloque timestamps antiguos sin zona explícita de Prop360 porque no hay garantía sobre su zona original.

## Restricciones del despliegue

No habilitar el flag de envío manual desde esta PR. Ejecutar primero las pruebas de compilación, autorización, proveedor simulado, deduplicado y regresión del chatbot/CRM. Después validar la integración real con un ejecutivo conectado, con consentimiento expreso, en un caso controlado. Solo entonces habilitar manual, y diseñar/validar el auto HOT y exención SLA como un PR independiente.
