# Operación unificada de campañas de rebajas

Todas las operaciones de descubrimiento, selección, preparación, aprobación, envío y reanudación se realizan desde `scripts/enviar_piloto_rebajas.py`. El selector `--pilot` apunta a las tres filas ya persistidas de `pilot_real_rebajas_20261109_v2` y valida que sean exactamente las propiedades 6436, 6377 y 16479 con sus precios aprobados. No descubre ni sustituye destinatarios.

El resto de la cartera se gestiona con `--interactive --full-scan`; se puede elegir `all` en el menú para seleccionar todas las rebajas y destinatarios elegibles que aún no tengan esa campaña. Cuando queda un lote aprobado pendiente, la siguiente ejecución ofrece revisarlo y reanudarlo antes de descubrir campañas nuevas. Los registros en `SENT`, `RESPONDED`, `DECLINED` u `OPTED_OUT` no vuelven a intentarse. `SENDING` y `DELIVERY_UNKNOWN` no se reenvían automáticamente.

`--max-messages` es el máximo de intentos permitidos por ejecución, incluso cuando el menú ofrece un número menor. Los pendientes permanecen en las mismas entradas de `leads.price_drop_v2.campaigns` y pueden continuar en la siguiente ejecución. Piloto y cartera pasan por el mismo ejecutor, verificación de publicación/precio, límites de WaSender, registro en `leads`, clasificación de respuestas y avisos administrativos.

Antes de operar, el entorno local debe tener WaSender configurado, `PRICE_DROP_LEADS_V2_SEND_ENABLED=true` y `PRICE_DROP_LEADS_V2_CONTEXT_ENABLED=true`. El estado `APPROVED` de cada fila, una autorización WhatsApp documentada y vigente (reutilizada desde el lead o recuperada de una campaña anterior con alcance explícito), la ausencia de baja/bloqueo, la publicación y precio vigentes, el límite diario y la confirmación final siguen siendo obligatorios. La consulta histórica demuestra relación comercial, no consentimiento. `PRICE_DROP_LEADS_V2_APPROVED_ID` queda reservado al bootstrap aislado de QA; no se fija un único ID para las campañas reales por lote. La aplicación web no inicia envíos de campañas reales al arrancar.

## Preparación de la cartera antes de iniciar nuevas campañas

El inventario se puede calcular **ahora, sin esperar los envíos del piloto**.
El informe es de solo lectura: no crea campañas, no marca consentimientos, no
modifica leads y no invoca al proveedor.

Ejecutar desde el clon local con las credenciales MongoDB ya configuradas:

```powershell
python .\scripts\enviar_piloto_rebajas.py --audience-audit --full-scan --temperature TODOS --lead-max-age-days 120
```

Este informe recorre todos los eventos de rebaja actualmente válidos y cruza
consultas previas, edad del lead, temperatura comercial, duplicados y
autorizaciones WhatsApp reutilizables previamente documentadas. Incluye:

- `eligible_event_matches` y `unique_eligible_leads`: consultas históricas
  que superan los filtros comerciales. **No equivalen a permiso de envío**.
- `unique_eligible_whatsapp_reusable_verified`: registros de autorización
  reutilizable que el motor puede verificar con teléfono, canal, alcance,
  evidencia, revisor, fecha y vigencia.
- `unique_eligible_whatsapp_evidence_review_required`: casos que necesitan
  revisar documentación antes de preparación.
- `unique_eligible_whatsapp_invalid_or_revoked`: registro existente, pero
  inválido, caducado o revocado. No debe reutilizarse.
- `whatsapp_review_queue_preview`: primeras 50 coincidencias para revisión
  con identificador parcial de lead y código de propiedad; no expone teléfonos
  ni conversaciones.

Las cifras corresponden a la base de datos leída en ese momento y deben
revalidarse antes de efectuar un envío. El inventario no certifica permisos
externos al CRM.

**Cuando existan autorizaciones verificables**, utilizar
`--interactive --full-scan --max-messages 20`, seleccionar eventos y
destinatarios y revisar el mensaje exacto. La herramienta conserva los
controles de preparación `PREPARAR N`, aprobación `APROBAR N` y envío
`ENVIAR N` por separado. Para preparar ahora y enviar después, no confirmar
`ENVIAR N`: los mensajes permanecen aprobados y pendientes. Para evitar
aprobar antes de revisar, detenerse en `APROBAR N`: las campañas permanecen
`PREPARED`.

No copiar la excepción de contacto del piloto `pilot_real_rebajas_20261109_v2`
a campañas de cartera: está limitada a las tres propiedades y precios exactos.
La consulta previa de una propiedad no equivale a una autorización general de
mensajes de rebaja.

## Comandos operativos

Ejecutar desde la raíz del repositorio en PowerShell:

```powershell
python scripts\enviar_piloto_rebajas.py --pilot --execute --max-messages 3
python scripts\enviar_piloto_rebajas.py --interactive --full-scan --max-messages 20
```

El segundo comando aplica el máximo de 20 intentos por ejecución y vuelve a preguntar por lotes aprobados pendientes en las ejecuciones sucesivas. El menú permite elegir `all`; ninguna fila se envía sin aprobación persistida y confirmación exacta del operador.

Los antiguos `scripts/price_drop_campaign_real_pilot.py` y `scripts/price_drop_leads_v2_real_pilot.py` se conservan como stubs que fallan cerrados y no abren base de datos ni proveedor. No deben usarse para preparar ni ejecutar campañas.
