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


## Fase 2 — implementación prospectiva HOT (18-11-2026)

La fase 2 está implementada en \`chatbot/crm_hot_linked_auto.py\` y conectada al ciclo de vida de \`webhook.py\`, pero permanece **inactiva por defecto**.

**Variables requeridas antes de una activación controlada:**

- \`PROCASA_EXEC_HOT_AUTO_ENABLED=1\` — interruptor maestro del envío automático HOT (valor predeterminado OFF).
- \`PROCASA_EXEC_HOT_AUTO_CUTOVER_AT=YYYY-MM-DDTHH:MM:SS-03:00\` (o equivalente con zona UTC) — fecha futura de activación. No se envían mensajes a ciclos asignados antes de ese instante.
- \`PROCASA_EXEC_HOT_AUTO_SESSION_IDS=133517,...\` — sesiones de ejecutivos autorizadas explícitamente para envío automático; independiente de la lista de captura y del envío manual.

**Reglas operativas:**

1. El worker revisa cada 30 segundos ciclos activos HOT nuevos y con antigüedad máxima de 60 minutos. No procesa cartera anterior al cutover ni leads NORMAL/COLD.
2. La identidad del ejecutivo, sesión, token privado, ciclo activo, propietario actual y teléfono deben coincidir. Un WhatsApp capturado no implica permiso para enviar.
3. Exige una solicitud de WhatsApp reciente (24 horas) con origen y proveedor verificables, o una autorización comercial explícita \`crm_hot_auto.whatsapp_contact_authorization\` con \`scope=SALE_INQUIRY\`, teléfono, evidencia, revisor, fecha y estado ACTIVE sin revocación. No se usan consentimientos de campañas promocionales como equivalente automático.
4. Un STOP/bloqueo del cliente, sesión no disponible, proveedor rechazando el mensaje, falta de \`provider_message_id\`, error o timeout **mantienen el SLA HOT existente**. Un timeout queda durable y no se reintenta automáticamente.
5. Solo tras la aceptación del proveedor con ID y escritura del mensaje en \`leads.messages\`, el ciclo obtiene \`hot_auto_sla_exemption\` con \`kind=HOT_EXEC_API_CONFIRMED\`, política \`hot_exec_auto_v1\`, remitente, sesión, ciclo, fecha UTC y referencia del mensaje.
6. Los mensajes automáticos se guardan como \`actor_type=automation\` y \`source=crm_hot_auto_api\`, no como \`human_agent\`. El saludo no marca \`first_valid_management_at\` ni concluye seguimiento.
7. \`calculate_cycle_sla\`, el monitor/revalidador de alertas, la comprobación de vencimiento, el filtro CAS de transacción y el ejecutor de reasignación reconocen la exención únicamente en ese ciclo activo. En CRM se distingue como **Sin SLA · WhatsApp automático**.
8. Para HOT no elegibles y todo NORMAL/COLD, el SLA vigente continúa idéntico.

**Límites:** Se requiere validación con una sesión real y autorización antes de habilitar estas variables en Render. Un HTTP 200 con ID es aceptación del proveedor, no prueba de lectura del cliente. La nueva condición de excepción no sustituye el control de seguimiento humano. Las conversaciones históricas no tienen backfill automático.
