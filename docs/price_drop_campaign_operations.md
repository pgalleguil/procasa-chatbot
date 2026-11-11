# Operación unificada de campañas de rebajas

Todas las operaciones de descubrimiento, selección, preparación, aprobación, envío y reanudación se realizan desde `scripts/enviar_piloto_rebajas.py`. El selector `--pilot` apunta a las tres filas ya persistidas de `pilot_real_rebajas_20261109_v2` y valida que sean exactamente las propiedades 6436, 6377 y 16479 con sus precios aprobados. No descubre ni sustituye destinatarios.

El resto de la cartera se gestiona con `--interactive --full-scan`; se puede elegir `all` en el menú para seleccionar todas las rebajas y destinatarios elegibles que aún no tengan esa campaña. Cuando queda un lote aprobado pendiente, la siguiente ejecución ofrece revisarlo y reanudarlo antes de descubrir campañas nuevas. Los registros en `SENT`, `RESPONDED`, `DECLINED` u `OPTED_OUT` no vuelven a intentarse. `SENDING` y `DELIVERY_UNKNOWN` no se reenvían automáticamente.

`--max-messages` es el máximo de intentos permitidos por ejecución, incluso cuando el menú ofrece un número menor. Los pendientes permanecen en las mismas entradas de `leads.price_drop_v2.campaigns` y pueden continuar en la siguiente ejecución. Piloto y cartera pasan por el mismo ejecutor, verificación de publicación/precio, límites de WaSender, registro en `leads`, clasificación de respuestas y avisos administrativos.

Antes de operar, el entorno local debe tener WaSender configurado, `PRICE_DROP_LEADS_V2_SEND_ENABLED=true` y `PRICE_DROP_LEADS_V2_CONTEXT_ENABLED=true`. El estado `APPROVED` de cada fila, la evidencia documentada de autorización WhatsApp, la ausencia de baja/bloqueo, la publicación y precio vigentes, el límite diario y la confirmación final siguen siendo obligatorios. `PRICE_DROP_LEADS_V2_APPROVED_ID` queda reservado al bootstrap aislado de QA; no se fija un único ID para las campañas reales por lote. La aplicación web no inicia envíos de campañas reales al arrancar.

## Comandos operativos

Ejecutar desde la raíz del repositorio en PowerShell:

```powershell
python scripts\enviar_piloto_rebajas.py --pilot --execute --max-messages 3
python scripts\enviar_piloto_rebajas.py --interactive --full-scan --max-messages 20
```

El segundo comando aplica el máximo de 20 intentos por ejecución y vuelve a preguntar por lotes aprobados pendientes en las ejecuciones sucesivas. El menú permite elegir `all`; ninguna fila se envía sin aprobación persistida y confirmación exacta del operador.

Los antiguos `scripts/price_drop_campaign_real_pilot.py` y `scripts/price_drop_leads_v2_real_pilot.py` se conservan como stubs que fallan cerrados y no abren base de datos ni proveedor. No deben usarse para preparar ni ejecutar campañas.
