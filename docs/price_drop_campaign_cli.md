# Campañas manuales de rebaja de precio

`scripts/enviar_piloto_rebajas.py` descubre rebajas vigentes en Prop360, muestra los leads con evidencia de consulta anterior y opera campañas V2 aprobadas. Se ejecuta desde PowerShell; no crea una app o proceso de fondo. Las escrituras de campaña quedan en `URLS.leads`; Prop360 y su historial son de solo lectura.

El flujo no descubre personas y las envía automáticamente. Cada evento se prepara y aprueba por separado, los mensajes exactos se muestran antes de enviar, y la terminal exige escribir `ENVIAR {campaign_id}`. El envío se bloquea en Render, ante una entrega incierta, si falta evidencia de autorización de WhatsApp o si no se pueden verificar el precio y el enlace vigentes.

## Configuración local

Usa un checkout actualizado y las credenciales guardadas en tu `.env` privado. El repositorio no incluye secretos. El proceso admite apuntar al `.env` existente sin copiarlo al checkout:

```powershell
$env:PRICE_DROP_ENV_FILE = 'C:\ruta\privada\procasa\.env'
```

Integra las variables de `.env.example` en ese archivo privado o apunta `PRICE_DROP_ENV_FILE` a otro archivo privado que incluya tanto las credenciales de Mongo/WaSender como los ajustes operativos. No sobrescribas tu `.env` existente. Los parámetros de la línea de comandos tienen prioridad sobre esos ajustes.

```powershell
python scripts/enviar_piloto_rebajas.py --help
```

Sin una acción, el modo predeterminado es `--dry-run` mientras `PRICE_DROP_DRY_RUN_DEFAULT=true`. `--dry-run`, `--discover`, `--status` y `--report` solo leen MongoDB y nunca llaman al proveedor de WhatsApp.

## Descubrir y revisar

```powershell
python scripts/enviar_piloto_rebajas.py --discover
python scripts/enviar_piloto_rebajas.py --dry-run
python scripts/enviar_piloto_rebajas.py --dry-run --property 16479
python scripts/enviar_piloto_rebajas.py --pilot --status
python scripts/enviar_piloto_rebajas.py --report
```

El descubrimiento descarta aumentos, conversiones de moneda, fechas inválidas, propiedades retiradas/no disponibles, publicaciones sin URL activa y precios históricos distintos del precio vigente. Un evento conserva moneda original UF o CLP y tiene identificador determinista. Si hubo varias reducciones, el mensaje incluye los pasos relevantes posteriores al último aviso encontrado.

Los candidatos se ordenan por elegibilidad y luego HOT, NORMAL y COLD. La temperatura prioriza la revisión; no sustituye la prueba de que el mensaje entrante refiere a esa publicación y precede a la rebaja. El programa enmascara teléfono e ID en las tablas.

## Preparar y aprobar una campaña futura

Primero identifica el `event_id` y los leads elegibles en `--dry-run`. Preparar requiere marcar la revisión de la consulta previa y la revisión de autorización específica para WhatsApp; la segunda referencia debe apuntar al registro de consentimiento/opt-in que revisaste en el CRM. No escribas el contenido completo de un consentimiento o datos personales en el campo de evidencia.

```powershell
python scripts/enviar_piloto_rebajas.py `
  --prepare `
  --event-id <EVENT_ID> `
  --lead-id <MONGO_LEAD_ID> `
  --operator "Supervisor" `
  --contact-basis-reviewed `
  --whatsapp-contact-reviewed `
  --whatsapp-contact-evidence "Referencia CRM <ID>, opt-in revisado <AAAA-MM-DD>" `
  --confirm-action PREPARAR_<EVENT_ID>

python scripts/enviar_piloto_rebajas.py `
  --approve `
  --campaign-id <EVENT_ID> `
  --lead-id <MONGO_LEAD_ID> `
  --operator "Supervisor" `
  --confirm-action APROBAR_<EVENT_ID>
```

El comando crea un respaldo local de los leads antes de escribir. Una campaña ya preparada, aprobada o enviada no se vuelve a preparar. Para actualizar la revisión humana de un registro aprobado que aún no fue enviado (por ejemplo, el piloto histórico), usa `--review-whatsapp-contact` con su lead ID, campaña y referencia de consentimiento; esto preserva la aprobación y no crea una fila nueva.

## Ejecutar o reanudar

Los bloqueos de V2 y el canal deben habilitarse solo en el equipo local, para la campaña elegida y después de verificar permiso de contacto. Los flags del ejemplo `.env.example` quedan `false`; nunca habilites el envío desde Render.

Antes de una ejecución autorizada, habilita los flags solo en la sesión PowerShell local. El ID de campaña y la lista de leads del piloto se mantienen fuera del repositorio:

```powershell
$env:PRICE_DROP_LEADS_V2_SEND_ENABLED = 'true'
$env:PRICE_DROP_LEADS_V2_APPROVED_ID = 'pilot_real_rebajas_20261109_v2'
$env:PRICE_DROP_LEADS_V2_CONTEXT_ENABLED = 'true'
$env:PRICE_DROP_LEADS_V2_REAL_PILOT_ENABLED = 'true'
$env:PRICE_DROP_LEADS_V2_REAL_PILOT_RECIPIENTS = '<LEAD_ID_1>:6436;<LEAD_ID_2>:6377;<LEAD_ID_3>:16479'
$env:PRICE_DROP_LEADS_V2_WHATSAPP_CONTACT_REVIEWED = 'true'
```

Para campañas futuras, usa `PRICE_DROP_LEADS_V2_REAL_CAMPAIGNS_ENABLED=true` y el ID exacto en `PRICE_DROP_LEADS_V2_APPROVED_ID`. Estos permisos no anulan las demás validaciones. Si el último mensaje entrante del lead tiene más de 24 horas, la herramienta bloquea el texto libre aunque la campaña esté aprobada; el sender actual no soporta plantillas.

El piloto histórico ya tiene tres filas aprobadas. No lo prepares de nuevo. Después de comprobar el registro de opt-in de cada lead, puedes guardar una referencia de revisión para los tres registros existentes con el siguiente comando. Esta acción modifica solo los tres documentos `leads`, hace respaldo local y no envía mensajes:

```powershell
python scripts/enviar_piloto_rebajas.py --pilot --review-whatsapp-contact `
  --operator "Supervisor" `
  --whatsapp-contact-evidence "Referencia CRM revisada para los tres leads; <ID/fecha>" `
  --confirm-action REVISAR_WHATSAPP_pilot_real_rebajas_20261109_v2
```

Piloto existente, exactamente tres propiedades y máximo de tres intentos:

```powershell
python scripts/enviar_piloto_rebajas.py --pilot --execute `
  --max-messages 3 --min-delay-seconds 300 --max-delay-seconds 600
```

Al finalizar, desactiva `PRICE_DROP_LEADS_V2_SEND_ENABLED` y `PRICE_DROP_LEADS_V2_REAL_PILOT_ENABLED`; deja activo el contexto solo durante su ventana de respuesta. V2 vuelve a bloquear la salida en Render aunque esas variables quedaran definidas por error.

Campaña futura previamente aprobada:

```powershell
python scripts/enviar_piloto_rebajas.py --execute `
  --campaign-id <CAMPAIGN_ID> `
  --max-messages 20 --min-delay-seconds 300 --max-delay-seconds 600
```

Continuar la única campaña activa, o precisar el ID si hay más de una:

```powershell
python scripts/enviar_piloto_rebajas.py --resume --max-messages 20
python scripts/enviar_piloto_rebajas.py --resume --campaign-id <CAMPAIGN_ID> --max-messages 20
```

Para consultar resultados:

```powershell
python scripts/enviar_piloto_rebajas.py --status --campaign-id <CAMPAIGN_ID>
python scripts/enviar_piloto_rebajas.py --report
```

El default es 20 mensajes por ejecución, con máximo permitido de 50. El plan Trial de WaSender se limita además a 50 solicitudes por día y requiere al menos 60 segundos entre solicitudes. El valor configurado por defecto es 300–600 segundos. En plan pagado, configura `PRICE_DROP_WASENDER_PLAN=paid` solo si tu cuenta realmente lo permite; se conserva un mínimo operativo de 5 segundos y un tope diario local configurable. Ningún intervalo garantiza aceptación, entrega, lectura ni ausencia de restricciones del proveedor.

Si el resultado es `DELIVERY_UNKNOWN`, `SENDING` o `HISTORY_RECONCILIATION_REQUIRED`, la secuencia se detiene y no reintenta. `--resume` primero reconcilia el recibo local de un mensaje aceptado con el documento original y `leads.messages`; si no hay evidencia suficiente, conserva el bloqueo para revisión manual. Un mensaje aceptado se registra como `assistant` con estado `accepted`; eso no significa que WaSender confirme que fue entregado o leído. El eco outbound del webhook se ignora y la reconciliación deduplica por ID del proveedor.

El contexto de respuesta V2 conserva su ventana de atribución vigente. El mensaje completo permanece en `leads.messages` después de que la ventana expira; una respuesta tardía no se atribuye automáticamente a una campaña antigua.

## Ventana y política de WhatsApp

Cada campaña necesita una revisión humana de la autorización para contactar por WhatsApp y una referencia de la evidencia almacenada en el CRM. Una consulta antigua de una propiedad no se trata como consentimiento general de marketing. El sender actual usa texto libre y no envía plantillas aprobadas, así que bloquea ambos modos (`official` y `wasenderapi`) si el último mensaje del cliente tiene más de 24 horas. La salida documentada de WaSender es un mensaje de texto plano. Consulta la [política de WhatsApp Business](https://business.whatsapp.com/policy/preview?lang=es_LA) y la [documentación de WaSender para mensajes de texto](https://www.wasenderapi.com/api-docs/) y [límites](https://www.wasenderapi.com/api-docs/rate-limits/understanding-rate-limits).

`PRICE_DROP_LEADS_V2_CONTEXT_ENABLED=true` permite que el webhook relacione respuestas recientes con el mensaje V2; no habilita envíos. Mantén los flags de envío apagados en Render. Después de una ejecución manual, desactiva `PRICE_DROP_LEADS_V2_SEND_ENABLED` y el flag de campaña; puedes conservar el contexto activo durante su ventana de respuesta.

Horario predeterminado: lunes a viernes, 09:00–19:00 `America/Santiago`. La zona IANA aplica los cambios estacionales de Chile. Una pausa que cruce el cierre detiene el lote y deja aprobados los pendientes.
