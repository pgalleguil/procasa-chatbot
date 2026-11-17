# WaSender ejecutivos — configuración sencilla y segura

## Decisión definitiva

**Una variable secreta Render por ejecutivo**, identificable por nombre. No hay JSON compartido con todas las sesiones: dar de baja un ejecutivo solo elimina su variable y desactiva su sesión. El servicio Render `procasa` es único para todos.

El chatbot opera **exclusivamente** desde el número de oficina. Los teléfonos de ejecutivos solo tienen auditoría pasiva y nunca envían ni responden por API.

## MongoDB (`URLS.usuarios`)

Únicamente se necesita el identificador de sesión WaSender:

```json
{"wasender": {"session_id": 133286}}
```

El nombre del ejecutivo se toma del campo existente `nombre`. **No** es necesario agregar un campo para claves o nombres de variables. Los campos antiguos `wasender.webhook_secret_env` y `wasender.api_token_env` (que actualmente pueden contener credenciales en texto claro) ya no se consultan y deben eliminarse mediante un `$unset` limitado a esos campos, una vez comprobada la nueva variable.

## Render — ejemplo para Pablo

Crear **una variable de entorno** llamada `PROCASA_WASENDER_PABLO_GALLEGUILLOS` y asignarle este JSON. Las claves mostradas son SOLO marcadores ficticios:

```json
{
  "session_id": 133286,
  "webhook_secret": "PEGA_SECRET_REAL_COMPLETO",
  "api_token": "PEGA_TOKEN_REAL_COMPLETO",
  "capture_enabled": false
}
```

La validación exige que `session_id` coincida con la ruta y la sesión registrada en MongoDB. No basta cambiar el nombre de la variable. Para otro ejecutivo, la variable es `PROCASA_WASENDER_` seguido del nombre completo normalizado en MAYÚSCULAS sin tildes y con guiones bajos, por ejemplo `PROCASA_WASENDER_MARIELA_ARRIAGADA`, `PROCASA_WASENDER_ROCIO_ALIAGA`.

Al añadir o eliminar ejecutivos, gestionar **solo su variable de Render**; no editar credenciales de otra sesión. El documento del ejecutivo debe estar activo (`is_active: true`) y el ID de sesión no debe aparecer en dos ejecutivos. Los nombres idénticos entre usuarios activos se rechazan para evitar identidad ambigua. Si se modifica el nombre de un usuario, se debe renombrar su variable de Render.

## Activación segura

- `PROCASA_EXEC_AUDIT_ENABLED=1` habilita la recepción del webhook; exige firma y vínculo de usuario único.
- `PROCASA_EXEC_AUDIT_WRITE_ENABLED=0` impide toda escritura. No cambiar durante la configuración inicial.
- `capture_enabled=false` en la variable individual impide la captura por defecto. También se puede usar la nueva variable no secreta `PROCASA_EXEC_AUDIT_CAPTURE_SESSION_IDS`: si existe, la lista de IDs separados por comas **reemplaza** a los valores `capture_enabled` de todas las sesiones. Solo las incluidas pueden almacenar mensajes; el formato inválido desactiva todas. Ejemplo: `133517,133523,133526,133527,133286`.
- Para guardar mensajes se requieren simultáneamente `PROCASA_EXEC_AUDIT_ENABLED=1`, `PROCASA_EXEC_AUDIT_WRITE_ENABLED=1`, la sesión autorizada por lista o JSON, webhook firmado, lead único asignado al ejecutivo y ciclo CRM vigente. No se permite ninguna escritura para números privados o leads ajenos.
- La lista permite activar o desactivar ejecutivos sin abrir ni sobrescribir tokens o secretos. No habilita la IA: `PROCASA_EXEC_INTELLIGENCE_ENABLED` sigue siendo independiente. No recupera automáticamente mensajes previos a la conexión o activación.

Ruta para Pablo: `POST https://procasa-chatbot-yr8d.onrender.com/webhook/ejecutivos/133286` y seleccionar `messages.upsert` + `messages.received` en WaSender. Nunca subir secretos reales a GitHub, MongoDB o chats.

Se deduplica por sesión + ID de mensaje, se conserva todo el historial del lead y se descartan grupos, privados, números sin lead y asignaciones obsoletas. La captura real en MongoDB sigue pendiente de validación controlada.
