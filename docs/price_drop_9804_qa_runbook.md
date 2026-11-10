# QA de rebaja PROCASA para el administrador 9804

Este flujo se limita a la propiedad 6436 y al registro canónico de prueba del administrador terminado en 9804. La reautorización solo atiende un registro histórico clasificado como búsqueda terminada; no levanta una baja explícita, otro bloqueo, ni autoriza contactos de clientes.

## Identidad y conservación

Configurar `PRICE_DROP_PILOT_TEST_PHONE` y `PRICE_DROP_LEADS_V2_ADMIN_PHONE` con el teléfono autorizado, y `PRICE_DROP_LEADS_V2_TEST_LEAD_ID` con el `_id` del lead que el webhook V2 resuelve de forma única. El código rechaza la preparación, el envío y la atribución entrante si la ruta canónica no identifica exactamente ese documento. No fusiona ni borra el duplicado en formato diferente.

La autorización añade un registro a `leads.price_drop_v2.qa_reauthorizations` con un ID de campaña único, evidencia de la aprobación del administrador, operador, caducidad de 24 horas y máximo de un mensaje. La sesión conserva `messages`, `campaigns`, `reply_events`, IDs procesados, `prospecto` y `conversation_id`; no modifica la supresión global. Al reclamar el único intento, la autorización queda consumida. Una baja explícita, entrega incierta o campaña no resuelta bloquean el flujo.

## Operación manual

No activar estas variables durante un despliegue normal. Mantener `PRICE_DROP_LEADS_V2_TEST_BOOTSTRAP_MODE=off`, `PRICE_DROP_LEADS_V2_SEND_ENABLED=false` y `PRICE_DROP_LEADS_V2_CONTEXT_ENABLED=false` mientras se despliega y verifica el código.

Para una autorización de QA aprobada expresamente por el administrador, configurar temporalmente:

```text
PRICE_DROP_LEADS_V2_TEST_BOOTSTRAP_MODE=reauthorize
PRICE_DROP_LEADS_V2_TEST_BOOTSTRAP_ID=<ID nuevo test_leads_6436_...>
PRICE_DROP_LEADS_V2_TEST_OPERATOR=<operador responsable>
PRICE_DROP_LEADS_V2_TEST_REAUTH_EVIDENCE=<referencia verificable a la aprobación explícita>
PRICE_DROP_LEADS_V2_TEST_LEAD_ID=<_id canónico verificado>
```

Este modo solo registra la autorización; no prepara ni envía. Revisar en los logs únicamente estado, campaña y motivo, nunca secretos ni el texto de aprobación. Después de confirmar `AUTHORIZED`, cambiar el modo a `preview` para obtener un preflight sin escrituras y verificar destinatario, UF 2.790 → UF 2.690, publicación y mensaje.

El envío requiere una decisión final separada: `PRICE_DROP_LEADS_V2_TEST_BOOTSTRAP_MODE=send`, `PRICE_DROP_LEADS_V2_TEST_BOOTSTRAP_ID` con el ID exacto aprobado, `PRICE_DROP_LEADS_V2_TEST_OPERATOR`, `PRICE_DROP_LEADS_V2_CONTEXT_ENABLED=true`, `PRICE_DROP_LEADS_V2_SEND_ENABLED=true` y `PRICE_DROP_LEADS_V2_APPROVED_ID` idéntico al ID. El bootstrap limita la llamada a la única identidad fijada, aprueba esa campaña y hace como máximo un intento; en `SENT`, `SENDING`, `FAILED` o `DELIVERY_UNKNOWN` no reintenta.

Tras la recepción, verificar el `provider_message_id`, el mensaje saliente en `leads.messages`, la respuesta en el mismo `_id`, `reply_events`, clasificación y aviso administrativo simulado. Desactivar inmediatamente `MODE`, `SEND_ENABLED`, `CONTEXT_ENABLED` y `APPROVED_ID`. La reautorización no constituye evidencia de consentimiento para los tres clientes del piloto.
