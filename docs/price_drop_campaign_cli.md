# Campañas manuales de rebaja de precio

`scripts/enviar_piloto_rebajas.py` consulta Prop360, identifica leads que preguntaron por la publicación antes de la rebaja y opera campañas V2 manuales desde PowerShell. La lectura usa `URLS.universo_cartera_prop360` y `URLS.universo_cartera_prop360_historial`; las escrituras de campaña quedan dentro de `URLS.leads`. No crea colecciones de campaña ni un proceso de fondo.

El piloto histórico de tres destinatarios se mantiene separado. El modo interactivo opera eventos nuevos, aprueba cada lead individualmente y exige una confirmación final antes de llamar al proveedor.

## Configuración local

Usa un checkout actualizado y apunta el programa al `.env` privado existente. No copies ese archivo al checkout ni sobrescribas sus valores:

```powershell
$env:PRICE_DROP_ENV_FILE = 'C:\ruta\privada\procasa\.env'
python scripts/enviar_piloto_rebajas.py --help
```

Integra los valores operativos de `.env.example` en un archivo privado. La línea de comandos prevalece sobre esos valores. El modo por defecto sin acción es `--dry-run`.

## Barrido y consultas de solo lectura

Sin `--limit`, el descubrimiento procesa el cursor completo en lotes de 500 documentos. No carga todo el historial en memoria. `--limit` es un límite diagnóstico opcional; `--full-scan` lo elimina. `--since`, `--property`, `--operation venta` y `--currency UF|CLP` conservan sus filtros.

```powershell
python scripts/enviar_piloto_rebajas.py --discover --full-scan
python scripts/enviar_piloto_rebajas.py --dry-run --full-scan --property 16479 --currency CLP
python scripts/enviar_piloto_rebajas.py --dry-run --since 2026-10-01T00:00:00-03:00 --operation venta --currency UF
python scripts/enviar_piloto_rebajas.py --pilot --status
python scripts/enviar_piloto_rebajas.py --report
```

`--discover`, `--dry-run`, `--status` y `--report` no escriben en MongoDB ni llaman al endpoint de envío. El menú calcula y descarta el conjunto de candidatos para cada evento, y solo vuelve a cargar los candidatos de las propiedades que el operador selecciona.

## Plan de expansión: piloto de 3 y después universo elegible completo

Las tres propiedades 6436, 6377 y 16479 son una **prueba acotada**, no el universo de campañas. Después se pueden revisar **todas las rebajas** de la cartera activa y todos los leads que consultaron la propiedad **antes de la rebaja**. No significa enviar indiscriminadamente a cada contacto del CRM: se excluyen búsquedas terminadas, bajas, teléfonos ambiguos, propiedades no vigentes y consultas sin evidencia.

**Auditoría general (solo lectura, sin envíos ni escrituras):**

```powershell
cd "C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok"
python scripts\enviar_piloto_rebajas.py --audience-audit --full-scan --temperature ALL --lead-max-age-days 0
python scripts\enviar_piloto_rebajas.py --discover --full-scan --temperature ALL --lead-max-age-days 0
python scripts\enviar_piloto_rebajas.py --summary
```

`--audience-audit` devuelve JSON: número total de eventos, propiedades, consultas históricas candidatas, coincidencias elegibles, clientes únicos elegibles, coincidencias repetidas entre eventos, revisiones pendientes y motivos de exclusión. Es un **inventario operativo**, no una habilitación de mensajes. No determina si existe autorización WhatsApp: ese campo declara `NOT_VERIFIED_BY_THIS_AUDIT`. Nunca almacena consentimientos ficticios ni altera `leads`.

Para preparar una campaña posterior al piloto, una vez revisado cada contacto y su consentimiento documentado:

```powershell
python scripts\enviar_piloto_rebajas.py --interactive --full-scan --temperature ALL --lead-max-age-days 0 --max-messages 20 --min-delay-seconds 300 --max-delay-seconds 600
```

El menú permite seleccionar `all` en eventos y luego en los destinatarios elegibles de cada evento, pero **cada destinatario** requiere verificación y referencia de consentimiento de WhatsApp. Si no existe, debe excluirse; una consulta histórica **no reemplaza** el consentimiento. Los envíos se fragmentan por jornada, según el límite real de la cuenta, y no se envían automáticamente al iniciar el programa.

**Secuencia de despliegue:** 1) piloto 3 leads con autorización y confirmación, 2) verificar chat entrante, rechazos, avisos admin/ejecutivo e idempotencia con registros reales, 3) auditar universo completo, 4) revisar consentimientos y preparar nuevos eventos, 5) aprobar mensajes por evento y ejecutar lotes pequeños, observando respuestas y bajas, 6) reanudar únicamente campañas aún aprobadas. Cada rebaja se identifica por evento distinto y se registra dentro del lead existente. El sistema no debe interpretar múltiples eventos para un mismo lead como autorización para bombardearlo: el operador debe evitar contactos concurrentes a un mismo número y respetar su preferencia de mensajes.

Antes de cualquier envío real, comprueba que el PAT de WaSender permite consultar los logs de **todas** las sesiones, que Render tiene habilitados el contexto V2 y los avisos correspondientes, y que los precios y URL siguen coincidiendo. El código de aviso al ejecutivo verifica el ciclo CRM activo y está separado del SLA. No se habilita ningún flag de emisión por esta auditoría.

## Flujo interactivo

Comando para abrir el menú completo y procesar todo el historial por lotes:

```powershell
python scripts/enviar_piloto_rebajas.py --interactive --full-scan --max-messages 20 --min-delay-seconds 300 --max-delay-seconds 600
```

El menú muestra los eventos con el total de elegibles y excluidos. El operador selecciona eventos y destinatarios con números, revisa las consultas verificadas y los motivos de exclusión, configura el máximo y las pausas, y ve todos los mensajes exactos. No se escribe nada hasta confirmar `PREPARAR N`; después de revisar el contenido persistido, `APROBAR N` aprueba cada campaña por lead. El envío requiere una tercera confirmación exacta: `ENVIAR N`.

Ejemplo: para elegir tres propiedades y hasta veinte destinatarios con pausas entre cinco y diez minutos, usa el mismo comando anterior, selecciona las propiedades y números de leads en pantalla, y deja los parámetros `20`, `300` y `600`. No se necesitan ObjectIds. El menú pide el nombre del operador y una referencia de consentimiento del CRM para cada teléfono seleccionado, mostrado con sus últimos cuatro dígitos. Los respaldos locales de los leads se crean antes de preparar y antes de aprobar.

Las campañas `PREPARED` que no se aprueben quedan pendientes sin envío. Las campañas `APPROVED` que no se envíen permanecen aprobadas. Una cancelación en cualquiera de esas etapas no invoca WaSender.

## Operación técnica separada

Se conservan los comandos avanzados. Para preparar y aprobar un lead individual:

```powershell
python scripts/enviar_piloto_rebajas.py `
  --prepare --event-id <EVENT_ID> --lead-id <MONGO_LEAD_ID> `
  --operator "Supervisor" --contact-basis-reviewed `
  --whatsapp-contact-reviewed `
  --whatsapp-contact-evidence "Referencia CRM opt-in <ID/fecha>" `
  --confirm-action PREPARAR_<EVENT_ID>

python scripts/enviar_piloto_rebajas.py `
  --approve --campaign-id <EVENT_ID> --lead-id <MONGO_LEAD_ID> `
  --operator "Supervisor" --confirm-action APROBAR_<EVENT_ID>
```

El operador debe revisar la autorización de cada lead. La consulta histórica de una propiedad demuestra la relación comercial, pero no se interpreta como consentimiento genérico de marketing. No incluyas el texto del consentimiento ni información personal en el campo de evidencia; usa una referencia del CRM.

Para ejecutar una campaña futura ya aprobada, el comando técnico sigue requiriendo los flags V2 locales habilitados para la campaña exacta. El menú interactivo los activa solo en el proceso local después de la confirmación final y los restaura al terminar. El envío se bloquea en Render en ambos casos.

## Piloto de tres destinatarios

El piloto ya existente no se reconstruye ni se incorpora al menú de campañas nuevas. Consulta primero su estado:

```powershell
python scripts/enviar_piloto_rebajas.py --pilot --status
```

Después de revisar el consentimiento individual en el CRM, se puede registrar una referencia en sus tres filas con `--pilot --review-whatsapp-contact`; el comando crea respaldo local y no envía. Para ejecutar posteriormente, conserva el control específico del piloto:

```powershell
python scripts/enviar_piloto_rebajas.py --pilot --execute `
  --max-messages 3 --min-delay-seconds 300 --max-delay-seconds 600
```

El piloto sigue sujeto a autorización revisada, estado aprobado, disponibilidad y precio actual, límites de cuenta, horario, confirmación final y reconciliación. Ningún envío se realizó durante el desarrollo.

## Revalidación de publicación y precio

Al preparar el menú y justo antes de cada intento, el programa vuelve a consultar disponibilidad, operación, precio y publicación activa en Prop360. Después solicita la URL pública por HTTPS y revisa sus metadatos estructurados de precio. Los resultados distinguen:

- `price_verified`: inventario y publicación coinciden.
- `link_accessible_price_unverifiable`: la página responde, pero no expone un precio estructurado verificable.
- `link_inaccessible`: error HTTP, fallo de red o host fuera de la lista permitida.
- `property_removed`: Prop360 ya no ofrece la propiedad en venta o la publicación devuelve 404/410.
- `inventory_public_inconsistent`: precio, moneda, oficina, operación o enlace cambiaron, o el precio público no coincide.

El envío se bloquea salvo `price_verified`. La lista inicial de hosts permitidos está en `.env.example`; se puede ampliar mediante `PRICE_DROP_PUBLIC_HOST_ALLOWLIST` para dominios inmobiliarios públicos aprobados. El verificador no sigue redirecciones fuera de esa lista.

## WaSender, consentimiento y ventana de mensajes

El sender que realmente usa V2 hace `POST /api/send-message` con `to` y `text` contra una sesión activa de WaSender. La documentación de WaSender describe mensajes de texto libre y no documenta una restricción técnica de 24 horas para ese endpoint. Por eso el programa ya no bloquea el modo `wasenderapi` solo por antigüedad del último mensaje entrante.

Esto no elimina ni reemplaza la autorización para contactar, las bajas, las exclusiones comerciales, ni los controles del proveedor y de WhatsApp. La política de Meta exige consentimiento explícito para comunicaciones por WhatsApp. Su regla de plantillas fuera de 24 horas se aplica a la Plataforma oficial de WhatsApp Business. Este repositorio no tiene un sender oficial ni envío de plantillas: seleccionar `PRICE_DROP_WHATSAPP_CHANNEL_MODE=official` se bloquea para evitar que esa etiqueta termine usando por error el sender WaSender. La documentación técnica del endpoint de texto de WaSender no declara que exima de políticas aplicables; el endpoint o la aceptación del proveedor tampoco garantizan entrega ni ausencia de restricciones.

Fuentes consultadas: [endpoint de texto de WaSender](https://api.wasenderapi.com/api-docs/messages/send-text-message), [política de mensajes de WhatsApp Business](https://business.whatsapp.com/policy/preview?lang=es_LA).

## Límites de WaSender y reconciliación

El máximo predeterminado es 20 mensajes por ejecución. Las pausas predeterminadas son 300–600 segundos. En Trial, la validación local exige al menos 60 segundos y no acepta un máximo diario superior a 50; el cap operativo de CLI predeterminado es 50. En Paid, el intervalo mínimo local es cinco segundos. Estos intervalos distribuyen la cadencia y no garantizan aceptación, entrega, lectura ni ausencia de restricciones.

Antes de enviar, el programa consulta las sesiones de la cuenta WaSender y pagina los registros de mensajes de todas ellas; cuenta registros de API del intervalo móvil de 24 horas para no limitar el cálculo a esta campaña. Esto requiere habilitar `log_messages` en cada sesión. Si alguna sesión no ofrece logs, faltan timestamps o la consulta no termina correctamente, el envío se bloquea. Los timestamps sin zona horaria se interpretan con `PRICE_DROP_WASENDER_LOG_TIMEZONE` (ejemplo `UTC`); confirma que coincide con la configuración que usa tu cuenta. WaSender documenta límites de Trial de 1 solicitud por minuto y 50 al día, límites de Paid por minuto, protección de cuenta de 1 solicitud cada cinco segundos y headers de límite; esos límites pueden cambiar. [Límites y headers de WaSender](https://www.wasenderapi.com/api-docs/rate-limits/understanding-rate-limits).

Esta lectura usa `WASENDER_PERSONAL_ACCESS_TOKEN` (PAT) para listar sesiones y consultar sus detalles de cuenta; después usa transitoriamente la clave API propia de cada sesión para leer sus `message-logs`. `WASENDER_TOKEN` sigue siendo la clave de la sesión activa para enviar mensajes. No se usa una credencial como fallback de otra; si falta el PAT o cualquier sesión no permite recuperar sus logs, la comprobación falla cerrada y el envío queda bloqueado. Las claves se mantienen solo en memoria y no se escriben en registros. Configura el PAT desde la cuenta WaSender sin incluirlo en el repositorio.

Si el resultado es `DELIVERY_UNKNOWN`, `SENDING` o `HISTORY_RECONCILIATION_REQUIRED`, la secuencia se detiene y no reenvía. `--resume` reconcilia primero el recibo local con el documento original y `leads.messages`; sin evidencia suficiente, mantiene el bloqueo para revisión manual. `accepted` confirma que el proveedor aceptó la solicitud, no que la entregó o leyó. El mensaje se conserva en el historial y el eco outbound del webhook se deduplica por ID del proveedor. El contexto V2 mantiene su ventana de atribución actual; una respuesta posterior no se atribuye automáticamente a una campaña antigua.

Horario predeterminado: lunes a viernes, 09:00–19:00 `America/Santiago`, con cambios de horario de verano IANA. Una pausa que cruce el cierre detiene la secuencia y deja aprobados los destinatarios pendientes.

## Filtros, resumen y progreso

El menú pregunta por temperatura y antigüedad. Se aceptan `HOT`, `NORMAL`, `COLD`, `HOT,NORMAL` y `TODOS`. Un lead sin temperatura reconocida aparece en `REVISIÓN_PENDIENTE` y requiere clasificación manual con referencia de CRM para entrar en una campaña. Esa revisión queda dentro de la campaña; no modifica la temperatura del lead.

La antigüedad usa solo `leads.created_at`. Los datetime BSON sin zona se interpretan como UTC; los strings ISO conservan su offset, y los ISO sin zona se interpretan en `America/Santiago`. Fechas ausentes, inválidas o futuras quedan pendientes de revisión. El menú propone 120 días por defecto; `0` selecciona expresamente antigüedad sin límite. El filtro se repite al descubrir, preparar y justo antes de cada intento. Al reanudar, no se puede ampliar la audiencia aprobada.

```powershell
python scripts/enviar_piloto_rebajas.py --dry-run --temperature HOT
python scripts/enviar_piloto_rebajas.py --dry-run --temperature HOT,NORMAL --lead-max-age-days 120
python scripts/enviar_piloto_rebajas.py --interactive --temperature HOT,NORMAL --lead-max-age-days 120
python scripts/enviar_piloto_rebajas.py --summary
python scripts/enviar_piloto_rebajas.py --summary --period-days 30 --temperature HOT,NORMAL
```

`--summary` es de solo lectura. Agrupa una fila por propiedad, muestra el evento vigente o el último evento registrado y evita contar dos veces a una misma persona dentro de un evento. Incluye elegibles, aceptaciones WaSender, entregas y lecturas verificadas, respuestas, interés, visitas, ofertas, llamadas, alternativas, bajas, búsquedas terminadas, fallos confirmados, entregas inciertas, campañas pendientes y la última actividad.

Durante el envío aparece una barra con intentos, aceptaciones WaSender, fallos confirmados, entregas/lecturas verificadas, respuestas, entregas inciertas y pendientes en el lote y en las campañas seleccionadas. `SENT` significa aceptación del proveedor; no significa entrega. Cada resultado se agrega y sincroniza en `%LOCALAPPDATA%\PROCASA\price_drop_execution_progress.jsonl`, junto al diario local de reconciliación, para conservar los contadores tras Ctrl+C. `--resume` procesa solo filas aprobadas que sigan pendientes; estados inciertos se reconcilian antes.

El nombre personalizado solo se toma de `prospecto.nombre` o `leads.nombre`, luego de pasar por el validador de nombres del CRM. No se usan nombres de propietarios, captadores o ejecutivos como comprador. Cada pareja cliente/evento obtiene una de tres variantes deterministas; todas conservan los mismos datos comerciales, enlace y mecanismo de baja. El texto final se persiste antes de aprobar y el mismo texto aceptado se registra en `URLS.leads.messages`.

Una rebaja nueva para una propiedad se muestra como evento separado. Antes de ejecutar, las campañas `PREPARED` o `APPROVED` de una rebaja anterior se marcan `SUPERSEDED`; los registros `SENT` y `RESPONDED` se conservan. No se reenvía automáticamente un destinatario aceptado ni uno con entrega incierta.

## Avisos administrativos V2

Las respuestas positivas siguen usando `send_real_admin_notice_once()` bajo `PRICE_DROP_LEADS_V2_REAL_ADMIN_NOTICES_ENABLED` y el teléfono allowlisted. El aviso incluye código, precios, teléfono completo para el administrador, intención, respuesta relevante y enlace autenticado al CRM. Una baja o búsqueda terminada activa un aviso informativo separado en `notifications.administrator_info`; solo se dirige al administrador autorizado, se registra una vez y deja vigente la exclusión global del cliente. Ambos caminos están deshabilitados por defecto; las pruebas usan sender simulado.
