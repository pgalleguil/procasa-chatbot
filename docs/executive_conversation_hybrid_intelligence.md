# Inteligencia híbrida de conversaciones comerciales de ejecutivos

## Seguridad y arquitectura
El chatbot opera solo en el teléfono de oficina. Los teléfonos de ejecutivos se usan exclusivamente para auditoría pasiva, sin respuestas automáticas.

La ruta firmada guarda mensajes en URLS.leads.messages únicamente cuando coinciden teléfono, sesión única, ejecutivo activo, lead inequívoco y ciclo CRM vigente. Grupos, chats ajenos y datos con identidad ambigua se descartan antes de inspeccionar texto.

El mismo update atómico que guarda el mensaje marca commercial_intelligence.pending=true junto a pending_message_id, pending_cycle_id, pending_session_id y pending_source. Esto crea una cola durable sin ralentizar al webhook.

Un worker con lease atómico de 90 segundos clasifica pendientes; comprueba otra vez la asignación activa, la sesión del usuario y el ciclo. El resultado solo se confirma con compare-and-swap del ID pendiente y el ciclo: una reasignación o un mensaje posterior invalidan la clasificación obsoleta.

## Motor híbrido: reglas + DeepSeek
1. Reglas determinísticas para visita explícita, llamada, oferta, alternativas, rechazo puntual, búsqueda terminada y baja global.
2. DeepSeek para interpretar necesidades del cliente con JSON estructurado y contexto limitado a 8 mensajes comerciales. El prompt elimina teléfono, correo y RUT; no incluye chats privados, nombres ni identificadores de clientes.
3. Consolidación bajo commercial_intelligence.summary con intent, search, search_status, contact, suggested_priority, campaign, source, confidence, needs_review, evidence_provider_ids, analyzed_at y versión.

Distinciones esenciales:
- OPT_OUT: petición explícita de no recibir más contactos.
- SEARCH_COMPLETED: ya compró, arrendó o dejó de buscar.
- PROPERTY_DECLINED: no quiere esa propiedad, pero la búsqueda sigue.
- ASK_ALTERNATIVES: solicita similares o cambia necesidades.
- ASK_VISIT / ASK_CONTACT / GIVE_OFFER: prioridad HOT sugerida; no se altera automáticamente la temperatura canónica del CRM.
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