# WaSender ejecutivos — configuración mínima (PROCASA)

## Objetivo

Un solo servicio Render `procasa`, una variable JSON para las credenciales de **todas** las sesiones y un único campo de identificación por usuario. El chatbot permanece exclusivamente en el número de oficina y no envía nada por teléfonos de ejecutivos.

## MongoDB: `URLS.usuarios`

Cada ejecutivo activo debe tener **solo** el dato público de vinculación:

```json
{"wasender": {"session_id": 133286}}
```

No registrar ni consultar credenciales en los documentos `usuarios`. Los campos legados `wasender.webhook_secret_env` y `wasender.api_token_env` pueden eliminarse **después de verificar** la variable nueva. Al eliminar, usar únicamente `$unset` sobre los dos subcampos, nunca sobrescribir todo `wasender`.

## Render: una variable para todas las sesiones

Nombre de variable: `PROCASA_EXEC_WASENDER_SESSIONS_JSON`.

Ejemplo estructural (marcadores ficticios, **no** copiar como secretos efectivos):

```json
{
  "133286": {
    "webhook_secret": "PEGAR_VALOR_REAL_EN_RENDER",
    "api_token": "PEGAR_VALOR_REAL_EN_RENDER",
    "capture_enabled": false
  },
  "222222": {
    "webhook_secret": "PEGAR_OTRO_SECRET_EN_RENDER",
    "api_token": "PEGAR_OTRO_TOKEN_EN_RENDER",
    "capture_enabled": false
  }
}
```

Se agrega una entrada al JSON por cada sesión vinculada. El identificador es el número de sesión y **no** el teléfono personal. No poner estos valores en el repositorio, conversaciones, documentos MongoDB ni registros. Todas las sesiones tienen su propio webhook secret y API token. El API token solo se usa para consultas de identidad `@lid`, nunca para enviar mensajes desde el teléfono de un ejecutivo.

## Controles de activación

- `PROCASA_EXEC_AUDIT_ENABLED=1`: permite leer el webhook de auditoría, siempre exigiendo firma válida y sesión única asociada a un usuario activo.
- `PROCASA_EXEC_AUDIT_WRITE_ENABLED=0`: valor inicial, no guarda contenido comercial.
- `capture_enabled=false` en cada entrada JSON: barrera por sesión para evitar que activar la escritura global afecte al resto del equipo. Para una prueba de almacenamiento, habilitar `true` **solo** en la sesión piloto y con `PROCASA_EXEC_AUDIT_WRITE_ENABLED=1`, siempre después de verificar lead canónico único y ciclo/propietario CRM.

Ruta: `POST /webhook/ejecutivos/{session_id}`. Para el piloto 133286: `https://procasa-chatbot-yr8d.onrender.com/webhook/ejecutivos/133286`. Mantener activados `messages.upsert` y `messages.received`.

El código rechaza eventos de grupos, chats privados no asociados al ejecutivo, leads ambiguos y cambios de ciclo. Deduplica eventos por sesión + `provider_message_id`. La captura comercial de texto debe verificarse en producción con un lead de prueba autorizado antes de habilitar a todo el equipo.
