# Inteligencia híbrida de conversaciones comerciales de ejecutivos

## Seguridad y arquitectura
El chatbot opera solo en el teléfono de oficina. Los teléfonos de ejecutivos se usan exclusivamente para auditoría pasiva, sin respuestas automáticas.

La ruta firmada guarda mensajes en URLS.leads.messages únicamente cuando coinciden teléfono, sesión única, ejecutivo activo, lead inequívoco y ciclo CRM vigente. Grupos, chats ajenos y datos con identidad ambigua se descartan antes de inspeccionar texto.

El mismo update atómico que guarda el mensaje marca commercial_intelligence.pending=true junto a pending_message_id, pending_cycle_id, pending_session_id y pending_source. Esto crea una cola durable sin ralentizar al webhook.

Un worker con lease atómico de 90 segundos clasifica pendientes; comprueba otra vez la asignación activa, la sesión del usuario y el ciclo. El resultado solo se confirma con compare-and-swap del ID pendiente y el ciclo: una reasignación o un mensaje posterior invalidan la clasificación obsoleta.

## Motor híbrido: reglas + DeepSeek
1. Reglas determinísticas para visita explícita, llamada, oferta, alternativas, rechazo puntual, búsqueda terminada y baja global.
2. DeepSeek para interpretar necesidades del cliente con JSON estructurado y hasta 32 mensajes comerciales recientes, además del resumen tipado anterior. El prompt elimina teléfono, correo y RUT; no incluye chats privados, nombres ni identificadores de clientes.
3. Consolidación bajo commercial_intelligence.summary con intent, search, search_status, contact, executive_management (case_type, next_step_hint, pending_customer_request, needs_review), campaign, source, confidence, needs_review, evidence_provider_ids, analyzed_at y versión.

Distinciones esenciales:
- OPT_OUT: petición explícita de no recibir más contactos.
- SEARCH_COMPLETED: ya compró, arrendó o dejó de buscar.
- PROPERTY_DECLINED: no quiere esa propiedad, pero la búsqueda sigue.
- ASK_ALTERNATIVES: solicita similares o cambia necesidades.
- ASK_VISIT / ASK_CONTACT / GIVE_OFFER: registrar **VISITA_SOLICITADA / CONTACTO_SOLICITADO / OFERTA_PROPUESTA**, con próximo paso sugerido y evidencia; nunca emitir HOT/NORMAL/COLD ni alterar la temperatura canónica o el SLA del chatbot.
- Mensaje del ejecutivo no es respuesta del cliente. El contador de respuestas exige secuencia ejecutivo -> cliente.

Las comunas y atributos extraídos por IA requieren evidencia textual en mensajes del cliente; no se aceptan deducciones derivadas de ofertas del ejecutivo.

## Limitaciones protectoras
La clasificación NO cambia SLA, asignaciones, estados de gestión ni preferencias efectivas de campañas. Para bajas o búsquedas terminadas genera una sugerencia que requiere revisión; campaign.enforced permanece false. Las decisiones automáticas de exclusión comercial deberán ser activadas mediante una etapa posterior y pruebas específicas.

Las clasificaciones se limitan al ciclo CRM actual, evitando que un ejecutivo nuevo herede indebidamente el análisis de conversaciones anteriores. Los errores de IA recurren a reglas y dejan needs_review=true sin exponer datos privados en logs.

## Render: controles independientes
- PROCASA_EXEC_AUDIT_ENABLED=1: recepción firmada.
- PROCASA_EXEC_AUDIT_WRITE_ENABLED=1 y capture_enabled=true en la variable de ese ejecutivo: almacenamiento de sus mensajes autorizados.
- PROCASA_EXEC_INTELLIGENCE_ENABLED=1: activa el trabajador. Por defecto 0.
- PROCASA_EXEC_INTELLIGENCE_LLM_ENABLED=1: activa DeepSeek para los mensajes de clientes; con 0 opera mediante reglas. Por defecto 0.

El worker procesa máximo tres leads por lote cada 20 segundos. Una caída del LLM no interrumpe WhatsApp, y la cola pendiente sobrevive al reinicio.

## Aceptación operativa antes de escalar
1. Validar pruebas unitarias y controles de regresión de la ruta firmada.
2. Habilitar el worker solo para el piloto de Pablo, sin tocar el chatbot de oficina.
3. Verificar un intercambio comercial real con lead correctamente asignado: messages, status=ready y resumen, sin duplicados ni conversaciones privadas.
4. Revisar manualmente las categorías de clasificación antes de habilitar las sesiones restantes.

WaSender no permite recuperar historial anterior al vínculo del teléfono.
## Regla de separación de clasificaciones

La temperatura **HOT / NORMAL / COLD** corresponde exclusivamente al tramo chatbot/ingreso y a su lógica canónica de asignación. La etapa posterior del ejecutivo tiene la clasificación independiente `commercial_intelligence.summary.executive_management` (caso comercial y acción sugerida), sin promoción a temperatura, gestión formal SLA, visitas realizadas ni ventas cerradas. Un `next_step_hint` es una sugerencia para seguimiento humano, nunca una acción automática.

## Historial durable de oportunidades comerciales (v5)

`URLS.leads.commercial_intelligence.opportunities` conserva los casos detectados por conversación comercial autorizada y por ciclo de asignación. Cada oportunidad tiene `opportunity_id` determinístico, ejecutivo, sesión, `assignment_cycle_id`, tipo comercial, estado, siguiente paso y referencias de los mensajes de evidencia (no copia contenido ni datos personales). El resumen de la conversación mantiene las oportunidades abiertas y el caso activo actual, aunque llegue después un saludo o un «gracias».

- `DETECTADA`: interés o nueva necesidad observada, sin dar por confirmada una gestión.
- `PENDIENTE_GESTION`: solicitud explícita de visita, contacto, oferta, información o alternativas. La respuesta de WhatsApp no basta para cerrar la solicitud.
- `EN_GESTION`: estado reservado para gestión comercial comprobada por un evento humano del CRM; el clasificador no lo atribuye por una promesa de WhatsApp.
- `CONCRETADA`: reservado para un resultado verificado del CRM; la IA no confirma por sí sola visitas ni aceptación de ofertas.
- `DESCARTADA`: la cancelación explícita del cliente puede cerrar el caso concreto, preservando el histórico; no cancela otras oportunidades.

Un mensaje saliente posterior solo agrega `agent_reply_observed` y su ID de evidencia. No registra gestión formal del CRM, no cambia el SLA ni la temperatura HOT/COLD. El ledger se actualiza con el mismo compare-and-swap de clasificación del documento `leads`, protegido por una revisión; retransmisiones y trabajadores concurrentes no deben crear duplicados. Los casos de ciclos anteriores permanecen históricos, pero no cuentan como oportunidades abiertas de una asignación nueva.

**Límite de escalamiento:** al estar integrado en el documento canónico de MongoDB, el historial de oportunidades debe vigilarse frente al límite de tamaño de 16 MB por documento. Para volúmenes extraordinarios se debe archivar en una colección de eventos con referencias dentro del lead antes de llegar al límite. Esta versión no implementa ese archivado ni una interfaz para transiciones manuales confirmadas.
