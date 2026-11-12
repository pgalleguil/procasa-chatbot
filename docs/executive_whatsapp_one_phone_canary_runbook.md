# ACTUALIZACIÓN: PRUEBA INICIAL SIN MONGODB — PROBE_ONLY

Existe un servicio gratuito separado:
- Render ID: srv-dbqplp0ae00c7393la60
- URL: https://procasa-executive-whatsapp-audit-canary.onrender.com
- Git: rama feat/executive-whatsapp-audit-precontract; autoDeploy desactivado.

**La primera prueba debe usar PROBE_ONLY, NO persistencia.**

Variables para habilitar la prueba de recepción de una única sesión:
- PROCASA_EXEC_AUDIT_ENABLED=true
- PROCASA_EXEC_AUDIT_PROBE_ONLY=true
- PROCASA_EXEC_AUDIT_PERSIST=false
- PROCASA_EXEC_AUDIT_PILOT_APPROVED=AUTHORIZED_ONE_PHONE_CANARY
- PROCASA_EXEC_AUDIT_BINDINGS_JSON={"canary01":{"session_id":"ID_REAL_DE_NUEVA_SESION","executive_user_id":"ID_CRM_VERIFICADO","webhook_secret":"SECRETO_REAL_DE_ESA_SESION"}}

No colocar claves reales en GitHub, correos, archivos de prueba o esta conversación. Configurar el JSON desde variables privadas de Render. La aplicación rechaza automáticamente más de una sesión.

En PROBE_ONLY, la aplicación **NO necesita URI de MongoDB**, no conoce ningún lead y no guarda contenido. Loguea exclusivamente conteos de mensajes válidos entrantes/salientes y descartados. Tampoco descarga archivos ni responde automáticamente.

La URL del webhook para el nuevo número piloto es:
https://procasa-executive-whatsapp-audit-canary.onrender.com/internal/executive-whatsapp/canary01/events

No habilitar esta URL para el número de oficina. No copiar el webhook de la oficina al nuevo número ni cambiar su configuración.

Procedimiento:
1. Obtener autorización expresa del usuario del teléfono piloto y aprobar el alcance de tratamiento de conversaciones.
2. Crear una sesión WaSender adicional; si sólo existe la de oficina, no reutilizarla ni desconectarla.
3. Vincular desde el móvil del ejecutivo mediante Dispositivos vinculados > Vincular un dispositivo > escanear el QR de la nueva sesión.
4. Obtener ID real de sesión y webhook secret desde WaSender, añadirlos a la variable JSON privada del servicio aislado de Render, activar el PROBE_ONLY y comprobar /health (capture_enabled=true, probe_only=true, storage_enabled=false).
5. Registrar la URL nueva en la configuración de webhook **sólo de la nueva sesión**, evento messages.upsert; conservar el secret idéntico.
6. Enviar a mano un mensaje de prueba desde un teléfono cliente autorizado y responder manualmente desde el móvil vinculado.
7. Verificar en logs de Render únicamente contadores incoming=1/outgoing=1, sin textos ni números. Revisar que no hubo ningún envío del sistema.
8. Si falla el envío manual, no asumir que funciona; inspeccionar payload de forma local y temporal con permisos, sin loguear PII.
9. Comprobar que el chatbot de oficina no cambió y que no hay escrituras al CRM/MongoDB.
10. Solo tras ese éxito evaluar activar persistencia para un único lead de prueba y DB de mínimo privilegio.

El servicio Render Free puede dormirse por inactividad. Despertarlo comprobando /health antes del canary y no confiar en él para capturar continuamente mensajes reales. Un plan pagado para captura continua requiere aprobación independiente.

**No conectar seis teléfonos en esta primera fase.**

---

# PROCASA — Manual de habilitación de un teléfono de ejecutivo

**Estado actual: código preparado en PR borrador #168; no fusionado y sin despliegue.**
**No vincular ninguno de los seis teléfonos hasta aprobar y ejecutar este procedimiento.**

## Arquitectura de seguridad

Se implementó una aplicación independiente: executive_whatsapp_standalone.py. 
El punto de entrada del chatbot sigue siendo webhook.py; **no importan ni comparten rutas ni clientes de envío**.
Para el canary recomendamos un servicio Render separado, en lugar de cambiar el
servicio productivo del chatbot. Esto puede tener costo y requiere autorización.

La app arranca cerrada: si no se configura explícitamente PROCASA_EXEC_AUDIT_ENABLED=true,
solo expone /health, no expone rutas de captura, no conecta MongoDB.

## Procedimiento de autorización y despliegue

**GATE A — revisión de código:** PR #168 revisada por segundo auditor; CI especial y 
Production CI Gate verdes para exactamente el SHA que se pretenda desplegar;
sin cambios en webhook.py, config.py, lógica CRM o SLA; pruebas del servicio aislado.

**GATE B — privacidad:** aprobación documentada para observar exclusivamente
conversaciones comerciales de un ejecutivo voluntariamente autorizado;
alcance, retención, acceso solo por administración y supresión de chats personales.
Esta aplicación NO verifica por sí sola la legitimidad de ese tratamiento.

**GATE C — infraestructura:** nuevo servicio aislado Render, no el servicio
procasa-chatbot-yr8d. Arranque: 
    uvicorn executive_whatsapp_standalone:app --host 0.0.0.0 --port ${PORT}
Configurar Python y dependencias desde requirements.txt; configurar salud en /health.
NO crear el servicio sin autorización por posible costo.

**GATE D — cuenta MongoDB técnica separada:** permisos mínimos de lectura en
crm_assignment_cycles y lectura/escritura solo en leads; no permiso de inserción
en leads ni escrituras sobre colecciones de asignación, eventos, resultados
de gestión, campañas o chatbot. Usar base URLS. Guardar credenciales solamente
como variables de entorno del servicio nuevo.

## Variables para UN teléfono, nunca seis simultáneamente

PROCASA_EXEC_AUDIT_ENABLED=true
PROCASA_EXEC_AUDIT_PERSIST=true
PROCASA_EXEC_AUDIT_PILOT_APPROVED=AUTHORIZED_ONE_PHONE_CANARY
PROCASA_EXEC_AUDIT_PRIVACY_REVIEW=approved
PROCASA_EXEC_AUDIT_DB=URLS
PROCASA_EXEC_AUDIT_MONGODB_URI=[secreto configurado solo en Render]
PROCASA_EXEC_AUDIT_BINDINGS_JSON=[JSON privado con UN alias, session_id, executive_user_id y webhook_secret]

El alias debe ser una cadena difícil de adivinar. El secreto nunca se pega
en repositorio, capturas de pantalla, logs o conversaciones.

**Importante:** la revisión de privacidad en la variable es un control técnico;
NO reemplaza una autorización legal real. Un operador autorizado debe
gestionar secretos y comprobar los permisos antes del despliegue.

## Configuración WaSender

1. Crear / habilitar solo una sesión adicional para un ejecutivo consentido.
2. No alterar la sesión de oficina que hoy atiende el chatbot.
3. En la sesión del ejecutivo, activar el evento messages.upsert.
4. Registrar únicamente la URL HTTPS del servicio aislado:
   https://HOST_AUDITORIA/internal/executive-whatsapp/ALIAS_AUTORIZADO/events
5. Configurar el mismo Webhook Secret que la sesión usa en la aplicación.
6. No configurar el chatbot ni otros eventos/sesiones en esta ruta.

## Canary de aceptación

1. Desde un teléfono de prueba autorizado, enviar un texto al ejecutivo.
2. El ejecutivo responde **manualmente** desde su teléfono WhatsApp Business.
3. Confirmar que el proveedor emite messages.upsert en ambas direcciones.
4. Verificar en el lead de prueba que se guardan dos mensajes con actor_type
   client y executive, session_id, timestamps, source y assignment_cycle_id.
5. Repetir el webhook y comprobar que no se agrega duplicado.
6. Probar una conversación personal no vinculada: cero mensajes guardados.
7. Probar un número de teléfono compartido por dos leads: cero asignaciones automáticas.
8. Probar fallos de MongoDB: no responder como guardado; verificar si WaSender
   reintenta de verdad. La documentación pública NO garantiza los reintentos.
9. Verificar que el chatbot de oficina mantiene su funcionamiento y que
   no se alteran SLA, gestión humana, campañas, temperatura ni asignaciones.
10. Contrastar manualmente 15–20 conversaciones reales con la ficha del CRM.
11. Determinar si aparecen mensajes anteriores al día de vinculación;
    registrar alcance y faltantes sin asumir recuperación íntegra.
12. Si no aparecen, usar el parser y el importador de exportaciones TXT
    con autorización, doble revisión y evidencia de ciclos.
13. Solo después de resultados aprobados, evaluar ampliación gradual a otras sesiones.

## Reversión inmediata

- Cambiar PROCASA_EXEC_AUDIT_ENABLED=false en el servicio separado, reiniciarlo
  y confirmar que la ruta de recepción responde 404.
- Si corresponde, desactivar el webhook exclusivamente en la sesión piloto.
- No tocar ni desconectar la sesión de oficina ni el servicio del chatbot.
- No borrar datos del lead sin procedimiento de retención y reversión aprobado.

## Estado real / limitaciones

- El receptor registra mensajes con escritura antes de ACK. Si el almacenamiento
  no se confirma, responde 503, pero el reintento del proveedor NO está garantizado.
  Confirmar esta función con WaSender antes de permitir uso continuo.
- En ausencia de timestamp original, solo acepta eventos frescos y marca
  la fecha como aproximada, no apta para evaluaciones de SLA.
- El clasificador comercial y el transporte DeepSeek están desacoplados del webhook;
  no se ejecutan automáticamente ni envían nada al cliente.
- El almacén de hallazgos en leads guarda candidatos pendientes de revisión humana.
- La capa semántica solo construye perfiles de preferencias; aún no crea embeddings.
- La política de acceso, retención y visualización segura en el CRM necesita
  implementación y aprobación para un uso productivo general.

## Condición final

**APTO para solicitar un piloto con UNA sesión** solo después de pasar A, B, C y D
y tener una ruta HTTPS independiente habilitada, con CI y revisión completos.
**NO APTO para conectar los seis teléfonos** hasta concluir el canary.

Documentación oficial:
https://wasenderapi.com/api-docs/webhooks/webhook-message-upsert/
https://wasenderapi.com/api-docs/webhooks/webhook-setup/
https://api-docs.deepseek.com/api/create-chat-completion/
