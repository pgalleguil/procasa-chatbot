# Piloto real de rebaja de precio — PROCASA

**Estado inicial: desactivado.** Este circuito es independiente del piloto único al 9804. NO usa envío masivo. El operador decide explícitamente cada ID de lead y aprueba la campaña por separado.

## Colecciones y datos

- `crm_price_drop_campaigns_v1`: cabecera, responsable de revisión, fecha de rebaja, propiedad, límite de destinatarios, intervalo, aprobador, contador de intentos y parada de emergencia
- `crm_price_drop_campaign_dispatches_v1`: **un documento por campaña+propiedad+lead**; ID del lead, teléfono, evidencia de consulta previa, auditoría de contacto, código, UF anterior/nueva, fecha y hora programadas, hora de intento, aceptada o incierta, provider ID, estado de entrega, interés, visitas, baja y estadísticas de respuestas
- `crm_price_drop_campaign_turns_v1`: mensajes del cliente y respuestas enviadas del chatbot asociados al despacho, fecha, provider ID, dirección, texto (máximo 1600 caracteres por mensaje), con deduplicación
- `leads.price_drop_campaign_v1`: únicamente una **marca comercial** de campañas recibidas, última propiedad, último estado, fecha y opt-out; no cambia propiedad, ejecutivo, ciclo, etapa, gestión humana ni SLA

Las conversaciones anteriores a la campaña continúan en `leads.messages`. Los nuevos mensajes se vinculan al despacho por el ID del proveedor y el teléfono. Revisar acceso de lectura a las colecciones de campaña porque contienen datos personales.

## Reglas que bloquean el envío

1. Audiencia de **1 a 5 IDs reales** revisados por un operador; sin consultas a toda la cartera ni selección automática
2. Campaña y propiedad válidas; venta activa PROCASA SUCRE con el precio actual coincidente con el informado
3. Un mensaje de **cliente** fechado antes de la rebaja dentro del mismo lead y código Prop360 coincidente (no basta con que el lead sea antiguo)
4. Teléfono único, válido, no sintético, sin bloqueo, sin baja, ni campaña previa para la misma propiedad
5. Revisión manual de la legitimidad del contacto (`consent_reviewed: true` y descripción `consent_basis`)
6. Ventanas futuras de lunes a viernes **10:00–18:00 hora Chile**, separadas al menos **10 minutos**. Pasados 45 min de la ventana no envía fuera de horario; requiere revisión
7. **Doble autorización:** aprobación persistida en la colección y variables `PRICE_DROP_REAL_PILOT_ENABLED=true` y `PRICE_DROP_REAL_PILOT_APPROVED_ID=<id exacto>`. El comando además requiere `--send`
8. Una llamada al proveedor por lead; si se desconoce resultado, `DELIVERY_UNKNOWN`, **sin reintentar automáticamente**
9. `--halt` detiene los próximos envíos aunque alguien llame `--send-due`

**Nunca definir esas dos variables de ejecución hasta decidir efectivamente el envío.** Por defecto no existe ningún trigger activo de arranque para el piloto real. Excepcionalmente puede habilitarse el arranque controlado con `PRICE_DROP_REAL_PILOT_START_ON_BOOT=true`; requiere también los otros dos flags, campaña aprobada y slots válidos, y la propia instancia únicamente ejecuta el control secuencial de ventanas. El script del teléfono 9804 es independiente.

## Archivo JSON local (no subir teléfonos, IDs ni consentimientos al repositorio)

Ejemplo de estructura, sustituyendo los datos con IDs reales tras auditoría:

```json
{
  "campaign_id": "sucre_6436_pilot_20261105",
  "property_code": "6436",
  "old_price_uf": 2790,
  "new_price_uf": 2690,
  "price_drop_at": "2026-10-06T17:00:00-03:00",
  "price_change_evidence": "Registro auditado Prop360 de cambio 2790 a 2690",
  "start_at": "2026-11-05T11:00:00-03:00",
  "gap_minutes": 12,
  "reviewed_by": "Supervisor Comercial",
  "recipients": [
    {
      "lead_id": "ID_MONGO_REVISADO",
      "consent_reviewed": true,
      "consent_basis": "Verificada comunicación previa y ausencia de baja"
    }
  ]
}
```

Los valores de fecha de rebaja del ejemplo **requieren cotejo con la bitácora original**; no pueden asumirse como definitivos. Tampoco hay leads precargados ni envío habilitado.

## Operación

**1. Vista previa, solo lectura**

```shell
python scripts/price_drop_campaign_real_pilot.py --file /ruta/segura/piloto.json
```

La respuesta indica cada slot, teléfono enmascarado, fecha de consulta y bloqueos. Ninguna operación de base de datos.

**2. Persistir el plan, sin enviar**

```shell
python scripts/price_drop_campaign_real_pilot.py --prepare --file /ruta/segura/piloto.json
```

**3. Aprobación independiente del plan**

```shell
python scripts/price_drop_campaign_real_pilot.py --approve sucre_6436_pilot_20261105 --operator supervisor
```

**4. Activar los permisos de envío y contexto, y ejecutar únicamente la ventana debida**

```shell
PRICE_DROP_REAL_PILOT_ENABLED=true
PRICE_DROP_REAL_PILOT_APPROVED_ID=sucre_6436_pilot_20261105
PRICE_DROP_REAL_PILOT_CONTEXT_ENABLED=true
python scripts/price_drop_campaign_real_pilot.py --send-due sucre_6436_pilot_20261105 --send
```

`--send-due` hace **máximo un intento**; volver a invocarlo únicamente en la siguiente ventana (ej., 11:12 y 11:24). Si no existe un programador externo, es manual; no se envía una campaña completa a la vez. No se activan envíos desde el arranque de Render.

Como alternativa para recorrer automáticamente las ventanas **solo mientras el operador mantenga abierto el proceso** (sin daemon y con las mismas protecciones), usar:

```shell
python scripts/price_drop_campaign_real_pilot.py --watch sucre_6436_pilot_20261105 --send
```

`--watch` exige los mismos permisos, solo arranca dentro de los 30 minutos previos a la primera ventana y se detiene al agotarse los intentos, vencer las ventanas o recibir `--halt`.

**Operación desde Render sin terminal:** después de aprobar la audiencia y confirmar el horario, activar `PRICE_DROP_REAL_PILOT_CONTEXT_ENABLED=true`, `PRICE_DROP_REAL_PILOT_ENABLED=true`, `PRICE_DROP_REAL_PILOT_APPROVED_ID` con el ID exacto y finalmente `PRICE_DROP_REAL_PILOT_START_ON_BOOT=true` antes del despliegue controlado. El arranque crea un runner de bajo impacto que dispara como máximo un destinatario por ventana. Tras finalizar, volver `PRICE_DROP_REAL_PILOT_START_ON_BOOT=false` y `PRICE_DROP_REAL_PILOT_ENABLED=false`, manteniendo `PRICE_DROP_REAL_PILOT_CONTEXT_ENABLED=true` durante siete días para el seguimiento de respuestas. **Jamás activar esto hoy ni antes de aprobación expresa del primer envío**. Mantener `PRICE_DROP_REAL_PILOT_CONTEXT_ENABLED=true` durante los siete días posteriores al envío aunque el permiso de *nuevos* envíos se desactive.

**5. Reporte de efectividad**

```shell
python scripts/price_drop_campaign_real_pilot.py --report sucre_6436_pilot_20261105
```

Incluye enviados, respondidos, tasa de respuesta, interés, solicitudes de visita, bajas y estados proveedor; no presume que una visita quedó agendada sin confirmación.

**6. Parada inmediata**

```shell
python scripts/price_drop_campaign_real_pilot.py --halt sucre_6436_pilot_20261105 --operator supervisor
```

Dejar además `PRICE_DROP_REAL_PILOT_ENABLED=false`. Las llamadas pendientes o de resultado desconocido **no** se reintentan.

## Verificaciones previas al primer cliente

- Revisar evidencia/fecha real del cambio de precio y estado actual de cada propiedad
- Revisar manualmente los 1–3 leads y el historial de su consulta
- Verificar consentimiento/pertinencia del seguimiento y objeciones anteriores
- Validar en una prueba controlada que el mensaje entrante conserva el contexto de la rebaja y que un interés de visita sigue el flujo CRM normal
- Confirmar recepción, conversación, resultados y baja en las colecciones correspondientes
- Supervisar entregas/status; pausar si hay un error o una respuesta con datos no confirmados
- Mantener restringida la lectura de ubicación y datos privados de Prop360. Los permisos de base de datos del chatbot aún requieren separación física por usuario/vista


## Preparación y aprobación desde Render (sin terminal)

La conexión MongoDB del asistente puede ser solo lectura. El servicio PROCASA prepara y aprueba el plan con sus credenciales normales y una activación puntual de su arranque.

1. Guardar el JSON revisado (IDs de leads, fechas, fundamentos; **sin teléfonos**) en la variable protegida `PRICE_DROP_REAL_PILOT_PLAN_JSON`.
2. Configurar `PRICE_DROP_REAL_PILOT_REVIEWER` igual a `reviewed_by` del JSON y `PRICE_DROP_REAL_PILOT_APPROVED_ID` igual al ID exacto de campaña.
3. Poner `PRICE_DROP_REAL_PILOT_BOOTSTRAP_MODE=preview` y realizar un despliegue controlado. Revisar `[PRICE_DROP_REAL_PILOT_BOOTSTRAP]` en los logs: número de elegibles, horarios, teléfonos enmascarados y bloqueos. **Sin escritura ni envío.**
4. Tras revisión, cambiar a `PRICE_DROP_REAL_PILOT_BOOTSTRAP_MODE=prepare` y desplegar. Se insertan cabecera y despachos `PLANNED`. **No envía WhatsApp.**
5. Tras la aprobación expresa, cambiar a `PRICE_DROP_REAL_PILOT_BOOTSTRAP_MODE=approve` y desplegar. Se marca la campaña `APPROVED`. **No envía WhatsApp.**
6. Volver a `PRICE_DROP_REAL_PILOT_BOOTSTRAP_MODE=off` y dejar apagados todos los permisos de envío.
7. Solo en la fecha autorizada, habilitar `PRICE_DROP_REAL_PILOT_ENABLED=true`, `PRICE_DROP_REAL_PILOT_CONTEXT_ENABLED=true` y `PRICE_DROP_REAL_PILOT_START_ON_BOOT=true`, y desplegar. El runner realizará **máximo un intento por ventana**, sin cambiar asignaciones ni SLA.
8. Tras las ventanas o ante una anomalía, volver `PRICE_DROP_REAL_PILOT_ENABLED=false`, `PRICE_DROP_REAL_PILOT_START_ON_BOOT=false` y `PRICE_DROP_REAL_PILOT_BOOTSTRAP_MODE=off`. Mantener únicamente `PRICE_DROP_REAL_PILOT_CONTEXT_ENABLED=true` durante siete días para auditar las respuestas.

**No activar prepare, approve ni el runner sin revisar primero la vista previa y autorizar expresamente cada etapa.**


## Clasificación exclusiva de campaña (no confundir con Hot del CRM)

**CRM y campaña son dos dimensiones distintas.** El CRM conserva su `lead_temperature_effective` y su historial `lifecycle.hot_since` sin intervención del clasificador de campaña. El seguimiento de la rebaja utiliza únicamente `campaign_response_classification` y `campaign_response_priority`, dentro de `crm_price_drop_campaign_dispatches_v1`. La marca `leads.price_drop_campaign_v1` también usa los nombres de campaña, nunca `temperature` ni `classification` genéricos.

| Mensaje tras el aviso | Etiqueta EXCLUSIVA de campaña | Prioridad de revisión |
| --- | --- | --- |
| Solicita visita, llamada u oferta | `PRIORIDAD_COMERCIAL` | Alta |
| Expresa interés general | `INTERES_DECLARADO` | Media |
| Hace preguntas informativas | `CONSULTA_INFORMATIVA` | Sin seguimiento obligatorio |
| No hay evidencia suficiente | `SIN_SENAL` | Sin seguimiento |
| Rechaza expresamente | `RECHAZADO` | Cerrar pendiente de campaña |
| Pide no recibir mensajes | `BAJA` | Bloquear campañas posteriores |

Solo se clasifican mensajes posteriores a un envío efectivamente aceptado por WhatsApp, durante los siete días de seguimiento y para el mismo teléfono, descartando menciones explícitas a otras propiedades. La correlación temporal **no demuestra por sí sola que el aviso haya causado la respuesta**: se documenta `campaign_attribution_basis=same_phone_after_accepted_send_window`.

### Distinguir los leads que ya eran Hot ANTES del aviso

Al seleccionar se conserva `crm_temperature_at_selection`; inmediatamente ANTES de llamar al proveedor se toma otra fotografía del CRM, `crm_temperature_before_send` y `crm_was_hot_before_send`. Así, un lead que entró Hot durante la mañana, antes de recibir la rebaja, queda como **Hot preexistente**, aunque después responda al mensaje.

Los reportes separan:

- `already_hot_before_send`: Hot del CRM al momento del envío (no es reactivación nueva).
- `positive_responses_after_notice`: respuesta positiva posterior al aviso, independientemente del estado previo.
- `positive_response_after_non_hot`: respuesta positiva posterior de quien tenía temperatura CRM conocida y **no** era Hot en el momento del envío; mide secuencia temporal, no causalidad.
- `priority_responses` e `declared_interest`: categorías propias del aviso, sin llamarlas Hot/Normal.
- `pending_human_review`: casos comerciales pendientes de revisión, no gestiones humanas realizadas.

Cada mensaje tiene su clasificación propia en `crm_price_drop_campaign_turns_v1` y conserva ID del proveedor y fecha. El despacho guarda `scheduled_at` (prevista), `attempted_at` (intento), `sent_at` (aceptación UTC), `sent_at_chile`, `first_response_at`, `first_positive_response_at` y `first_priority_response_at`. `SENT` confirma aceptación del proveedor, **no** entrega al destinatario.

Para revisar las respuestas comerciales en orden de prioridad, sin alterar CRM ni SLA:

```shell
python scripts/price_drop_campaign_real_pilot.py --worklist ID_DE_CAMPAÑA
```

Para comparar estados previos, horas, respuestas y resultados:

```shell
python scripts/price_drop_campaign_real_pilot.py --report ID_DE_CAMPAÑA
```

**Estas operaciones son solo lectura.** `PENDIENTE_REVISION` no implica contacto humano, gestión efectiva, visita confirmada ni inicio/cierre de SLA. La responsabilidad de clasificar y gestionar la temperatura oficial permanece en las rutas normales del CRM.

## Aviso de respuesta al ejecutivo por WhatsApp

- Una respuesta de campaña con interés declarado o prioridad comercial crea un único registro en `crm_price_drop_campaign_exec_notices_v1`. Si el interés escala a solicitud de visita, llamada u oferta, puede emitirse un segundo aviso de prioridad. Si ambos ocurren antes del primer envío se notifica solo la prioridad.
- El destinatario se valida usando el **ciclo de asignación vigente** del CRM. Ante conflicto, el aviso queda `BLOCKED_ASSIGNMENT`, sin envío ni reasignación.
- El mensaje identifica expresamente la **campaña de rebaja de precio**, código, UF anterior/actual y lead, sin dirección exacta ni atributos privados.
- El envío está apagado por defecto; requiere `PRICE_DROP_CAMPAIGN_EXEC_NOTIFY_ENABLED=true` y `PRICE_DROP_REAL_PILOT_CONTEXT_ENABLED=true`. La cola procesa como máximo un aviso por pasada del loop, normalmente cada 60 segundos.
- Estados: `PENDING`, `CLAIMED`, `SENT`, `DELIVERY_UNKNOWN`, `BLOCKED_ASSIGNMENT`, `SKIPPED`. No hay reintentos automáticos ante entrega desconocida. `SENT` indica aceptación del proveedor y no confirma entrega al ejecutivo.
- No afecta temperatura oficial, SLA, asignación ni crea otro lead. Mantener el interruptor apagado hasta la autorización de la prueba comercial.



## Enlace CRM y último ejecutivo tras redistribuciones

Las notificaciones de respuestas positivas de la campaña incorporan el mismo **enlace autenticado y seguro** que los avisos de nuevos leads: `build_secure_crm_url(lead)`, con ruta `/crm/lead-id/<ID Mongo del lead>`. No se coloca el teléfono del cliente en la URL; el CRM comprueba el acceso del ejecutivo.

El destinatario se resuelve **justo al procesar el aviso** desde el **único ciclo de asignación activo** de `crm_assignment_cycles`, no desde el primer ejecutivo, captador de la ficha ni el responsable anotado cuando se programó la campaña. Después de una redistribución válida, gana el último titular vigente del ciclo, siempre que los campos espejo del lead y el usuario activo coincidan.

Si hay conflicto, dos ciclos activos, titular dado de baja, usuario inexistente, identidad discrepante o una nueva redistribución justo antes del envío, se bloquea la alerta (sin enviarla al anterior). No se permite seleccionar destinatarios basándose únicamente en nombres. El caso queda en la cola para revisión del supervisor.

La leyenda del WhatsApp indica que se trata del **seguimiento a una rebaja de precio**, no de un lead nuevo, e incluye el código de propiedad, precio anterior y nuevo, intención comercial específica y enlace directo CRM. No contiene dirección exacta ni atributos sensibles de Prop360.

Nota semántica: `último ejecutivo` para estos avisos significa **titular del último ciclo de asignación vigente**. Un actor humano que registró una gestión en un ciclo antiguo ya cerrado no prevalece sobre una redistribución posterior.

## Incidente de la prueba V10 del 4 de noviembre: interés detectado pero aviso no enviado

En la conversación aislada del número autorizado, el cliente escribió `OK, ME INTERESA` y luego `SI, EL DIA DOMINGO DURANTE EN LA MAAÑANA`. El primer detector de pruebas exigía un `Sí` casi literal a la última pregunta; por ello no se intentó el WhatsApp simulado al ejecutivo.

La corrección reconoce interés explícito y solicitudes de visita/contacto mediante el clasificador exclusivo de campaña, además de afirmaciones contextualizadas con hora o día. No interpreta preguntas neutrales, negaciones ni simples saludos como interés.

Para recuperar **solamente el aviso V10 omitido**, sin reenviar la rebaja y sin consultar leads ni asignaciones del CRM, se dispone de `recover_v10_exec_notice_once(db)`. La tarea de arranque se ejecuta exclusivamente cuando `PRICE_DROP_PILOT_EXEC_RECOVER_ON_START=true` y `PRICE_DROP_PILOT_EXEC_NOTICE_SIMULATION=true`; también requiere el teléfono permitido por `PRICE_DROP_PILOT_TEST_PHONE` y la existencia de una V10 enviada, vigente y sin `lead_id`.

La recuperación inspecciona únicamente el histórico aislado V10 de ese teléfono, detecta la primera expresión de interés y envía una sola notificación **al mismo teléfono de prueba**, rotulada `PRUEBA INTERNA — AVISO SIMULADO AL EJECUTIVO`. Reserva un identificador único antes de contactar al proveedor y no reintenta resultados inciertos.

Después de verificar la aceptación por el proveedor, volver `PRICE_DROP_PILOT_EXEC_RECOVER_ON_START=false`. Los flags de campaña real y aviso a ejecutivos reales siguen `false`. Una prueba aislada no puede ni debe validar el nombre ni el teléfono del verdadero último ejecutivo: esa parte está cubierta por pruebas separadas de ciclos de asignación.
