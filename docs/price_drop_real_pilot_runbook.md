# Piloto real de rebaja de precio — PROCASA

**Estado inicial: desactivado.** Este circuito es independiente del piloto único al 9804. NO usa envío masivo. El operador decide explícitamente cada ID de lead y aprueba la campaña por separado.

## Colecciones y datos

- \`crm_price_drop_campaigns_v1\`: cabecera, responsable de revisión, fecha de rebaja, propiedad, límite de destinatarios, intervalo, aprobador, contador de intentos y parada de emergencia
- \`crm_price_drop_campaign_dispatches_v1\`: **un documento por campaña+propiedad+lead**; ID del lead, teléfono, evidencia de consulta previa, auditoría de contacto, código, UF anterior/nueva, fecha y hora programadas, hora de intento, aceptada o incierta, provider ID, estado de entrega, interés, visitas, baja y estadísticas de respuestas
- \`crm_price_drop_campaign_turns_v1\`: mensajes del cliente y respuestas enviadas del chatbot asociados al despacho, fecha, provider ID, dirección, texto (máximo 1600 caracteres por mensaje), con deduplicación
- \`leads.price_drop_campaign_v1\`: únicamente una **marca comercial** de campañas recibidas, última propiedad, último estado, fecha y opt-out; no cambia propiedad, ejecutivo, ciclo, etapa, gestión humana ni SLA

Las conversaciones anteriores a la campaña continúan en \`leads.messages\`. Los nuevos mensajes se vinculan al despacho por el ID del proveedor y el teléfono. Revisar acceso de lectura a las colecciones de campaña porque contienen datos personales.

## Reglas que bloquean el envío

1. Audiencia de **1 a 5 IDs reales** revisados por un operador; sin consultas a toda la cartera ni selección automática
2. Campaña y propiedad válidas; venta activa PROCASA SUCRE con el precio actual coincidente con el informado
3. Un mensaje de **cliente** fechado antes de la rebaja dentro del mismo lead y código Prop360 coincidente (no basta con que el lead sea antiguo)
4. Teléfono único, válido, no sintético, sin bloqueo, sin baja, ni campaña previa para la misma propiedad
5. Revisión manual de la legitimidad del contacto (\`consent_reviewed: true\` y descripción \`consent_basis\`)
6. Ventanas futuras de lunes a viernes **10:00–18:00 hora Chile**, separadas al menos **10 minutos**. Pasados 45 min de la ventana no envía fuera de horario; requiere revisión
7. **Doble autorización:** aprobación persistida en la colección y variables \`PRICE_DROP_REAL_PILOT_ENABLED=true\` y \`PRICE_DROP_REAL_PILOT_APPROVED_ID=<id exacto>\`. El comando además requiere \`--send\`
8. Una llamada al proveedor por lead; si se desconoce resultado, \`DELIVERY_UNKNOWN\`, **sin reintentar automáticamente**
9. \`--halt\` detiene los próximos envíos aunque alguien llame \`--send-due\`

**Nunca definir esas dos variables de ejecución hasta decidir efectivamente el envío.** No existe un trigger de arranque automático para el piloto real. El script del teléfono 9804 es independiente.

## Archivo JSON local (no subir teléfonos, IDs ni consentimientos al repositorio)

Ejemplo de estructura, sustituyendo los datos con IDs reales tras auditoría:

\`\`\`json
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
\`\`\`

Los valores de fecha de rebaja del ejemplo **requieren cotejo con la bitácora original**; no pueden asumirse como definitivos. Tampoco hay leads precargados ni envío habilitado.

## Operación

**1. Vista previa, solo lectura**

\`\`\`shell
python scripts/price_drop_campaign_real_pilot.py --file /ruta/segura/piloto.json
\`\`\`

La respuesta indica cada slot, teléfono enmascarado, fecha de consulta y bloqueos. Ninguna operación de base de datos.

**2. Persistir el plan, sin enviar**

\`\`\`shell
python scripts/price_drop_campaign_real_pilot.py --prepare --file /ruta/segura/piloto.json
\`\`\`

**3. Aprobación independiente del plan**

\`\`\`shell
python scripts/price_drop_campaign_real_pilot.py --approve sucre_6436_pilot_20261105 --operator supervisor
\`\`\`

**4. Activar los 2 permisos en el entorno seguro y ejecutar únicamente la ventana debida**

\`\`\`shell
PRICE_DROP_REAL_PILOT_ENABLED=true
PRICE_DROP_REAL_PILOT_APPROVED_ID=sucre_6436_pilot_20261105
python scripts/price_drop_campaign_real_pilot.py --send-due sucre_6436_pilot_20261105 --send
\`\`\`

\`--send-due\` hace **máximo un intento**; volver a invocarlo únicamente en la siguiente ventana (ej., 11:12 y 11:24). Si no existe un programador externo, es manual; no se envía una campaña completa a la vez. No se activan envíos desde el arranque de Render.

**5. Reporte de efectividad**

\`\`\`shell
python scripts/price_drop_campaign_real_pilot.py --report sucre_6436_pilot_20261105
\`\`\`

Incluye enviados, respondidos, tasa de respuesta, interés, solicitudes de visita, bajas y estados proveedor; no presume que una visita quedó agendada sin confirmación.

**6. Parada inmediata**

\`\`\`shell
python scripts/price_drop_campaign_real_pilot.py --halt sucre_6436_pilot_20261105 --operator supervisor
\`\`\`

Dejar además \`PRICE_DROP_REAL_PILOT_ENABLED=false\`. Las llamadas pendientes o de resultado desconocido **no** se reintentan.

## Verificaciones previas al primer cliente

- Revisar evidencia/fecha real del cambio de precio y estado actual de cada propiedad
- Revisar manualmente los 1–3 leads y el historial de su consulta
- Verificar consentimiento/pertinencia del seguimiento y objeciones anteriores
- Validar en una prueba controlada que el mensaje entrante conserva el contexto de la rebaja y que un interés de visita sigue el flujo CRM normal
- Confirmar recepción, conversación, resultados y baja en las colecciones correspondientes
- Supervisar entregas/status; pausar si hay un error o una respuesta con datos no confirmados
- Mantener restringida la lectura de ubicación y datos privados de Prop360. Los permisos de base de datos del chatbot aún requieren separación física por usuario/vista
