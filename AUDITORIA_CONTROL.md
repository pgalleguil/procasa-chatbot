# Auditoría técnica — Control PROCASA

## Propósito y alcance de esta rama

Esta rama contiene una instantánea de solo lectura del código local disponible el 16-10-2026 para inspeccionar las tres vistas Control: **Prospectos**, **Cartera** y **Captación**. El repositorio base de la rama es `main` en `1d4bdb0d0cf597ef9944793e1999f4bfdc752ca8`; los archivos actuales del árbol de trabajo se guardan aparte en `control-audit-snapshot/source/` para que la auditoría vea las implementaciones locales aunque no estén en `main`.

La aplicación ejecutable en la raíz de la rama no se ha alterado. Los archivos en `control-audit-snapshot/source/` son copias de referencia, no módulos conectados al runtime de la rama. El único archivo agregado fuera del snapshot es esta documentación. No se ejecutaron operaciones sobre MongoDB, servicios externos ni despliegues.

## Mapa de vistas y frontend

| Parte | Archivo de referencia | Responsabilidad visible en el código |
|---|---|---|
| Las tres vistas de Control y sus componentes | `control-audit-snapshot/source/templates/leads_dashboard.html` | Filtros compartidos de período/compare/ejecutivo; navegación Control; vistas Prospectos/Cartera/Captación; tarjetas, tablas, filtros secundarios, render, orden, paginación y llamadas `fetch` a los endpoints descritos abajo. |
| Vista previa local | `control-audit-snapshot/source/preview_dashboard.py` | Aplicación de previsualización local y rutas de lectura que alimentan el HTML. Su configuración local y credenciales no se incluyen. |
| Rutas del backend integrado | `control-audit-snapshot/source/endpoints/webhook_control_routes.py.txt` | Extracto literal de las rutas Control en `webhook.py` (líneas 2135–2350 del árbol inspeccionado), incluidas autorización y delegación al servicio. No es un módulo ejecutable. |

El HTML contiene más áreas del dashboard porque comparte plantilla y navegación; esta instantánea no cambia ni reconfigura otras pestañas.

## Flujo de datos y endpoints

La plantilla envía el ejecutivo y el rango ya resuelto por los controles existentes. Para fechas de comparación transmite además `compare` y `period_preset` al endpoint mensual; el backend no crea un segundo juego de controles de interfaz. La capa de servicio delega las consultas a `analytics/leads_queries.py`.

| Endpoint | Consumidor/resultado |
|---|---|
| `GET /api/leads-dashboard/operations/control-breakdown` | Vista de inventario/cartera vigente e historial recibido. |
| `GET /api/leads-dashboard/operations/control-monthly` | Cuatro tarjetas existentes, comparación y detalle agregado para Prospectos. |
| `GET /api/leads-dashboard/operations/control-monthly/executives` | Universo/ranking ejecutivo correspondiente al intervalo. |
| `GET /api/leads-dashboard/operations/control-portfolio-demand` | Demanda CRM por propiedad para Cartera. |
| `GET /api/leads-dashboard/operations/control-portfolio-property-leads` | Leads e historial asociados a una propiedad. |
| `GET /api/leads-dashboard/operations/control-portfolio-conversation` | Evidencia conversacional de lead/propiedad/ciclo. |
| `GET`, `POST`, `PATCH /api/leads-dashboard/control-commitments` | Lectura y persistencia de compromisos de reunión; POST/PATCH escriben al usarse. Solo se documenta el código; no se invocaron. |
| `GET /api/leads-dashboard/captacion-management` | Indicadores de gestión de captación para el ejecutivo/rango. |

Las rutas integradas requieren autenticación y roles privilegiados según el extracto. Los handlers consultan `analytics/leads_service.py`, `analytics/control_commitments.py` y `captacion_goals.py`, normalmente fuera del event loop mediante ejecutor de hilos. `preview_dashboard.py` ofrece rutas de previsualización local sin sustituir el backend integrado.

## Mapa de cálculos e indicadores

### Prospectos y tarjetas Control

- `analytics/monthly_control.py` define la estructura mensual y los cálculos agregados. `build_month_metrics` consume episodios, aplica exclusiones de prueba/reparación documentadas en el código, vincula gestión acreditada y deriva numeradores, denominadores y métricas comparables.
- `analytics/leads_queries.py` carga y filtra los episodios históricos, materializa filas de detalle, calcula las ventanas principal/comparable y arma la demanda por propiedad. Funciones relevantes: `query_monthly_leads_control`, `query_monthly_control_portfolio_demand`, `query_monthly_control_property_leads`, `query_monthly_control_lead_conversation`, además de los auxiliares `_control_*` de identidad, atribución y deduplicación.
- `analytics/leads_service.py` es la capa de delegación/cache entre rutas y consultas; revisar las funciones `get_monthly_leads_control`, `get_leads_control_breakdown`, `get_monthly_control_portfolio_demand`, `get_monthly_control_property_leads` y `get_monthly_control_lead_conversation`.
- **Episodio de asignación:** ciclo CRM identificado por `assignment_cycle_id`; se cuenta una vez por ciclo, no como lead único. El momento de cohorte es `assigned_at`, y el responsable es el ejecutivo histórico del episodio. Un lead puede tener más de un ciclo.
- **Lead único:** entidad CRM/lead deduplicada con los criterios de identidad definidos en consulta; no es intercambiable con episodios ni con eventos de ingesta.
- **Primera gestión:** evidencia humana válida asociada al mismo ciclo, resuelta por `chatbot/crm_metrics.py` y las reglas de gestión en `chatbot/crm_management.py`. La marca se compara con la ventana SLA persistida; el helper de minutos hábiles está en `chatbot/utils.py` y calendario/horario en `chatbot/business_calendar.py`.
- **SLA:** `calculate_sla` de `chatbot/crm_metrics.py` calcula estado y minutos con política/temperatura del episodio y calendario hábil; la capa de decisión productiva de asignación y protección se documenta en la siguiente sección. La métrica de Control no debe confundirse con transferencia ejecutada.
- **Contacto efectivo registrado:** la clasificación canónica CRM está en los tipos/resultados de `chatbot/crm_metrics.py`; el conteo analítico deduplica episodios/lead según la función concreta del dashboard. Un resultado CRM no demuestra por sí solo entrega, lectura o conversación bilateral.
- **Comparables:** la ventana comparable y cobertura están representadas en `monthly_control.py` y se pasan/renderizan desde el endpoint mensual. La tarjeta oculta variaciones cuando el modo es sin comparación o la cobertura/definición no permite comparación; consultar código para reglas exactas de cada métrica.

### Cartera y demanda

- La vista combina el inventario actual resuelto por identidad canónica de propiedad con demanda/episodios CRM dentro del período. Las responsabilidades de inventario son estado actual y no prueban titularidad histórica.
- `analytics/pricing_intelligence/property_identity.py` contiene normalización/alias de códigos y vínculos de propiedad; `snapshot_builder.py`, `snapshot_repository.py`, `lead_linkage.py`, `models.py` y `time_utils.py` dan soporte a snapshots, historial de precio y enlace temporal/identidad.
- `query_monthly_control_portfolio_demand` devuelve filas por código canónico, demanda CRM del período con vínculo aceptado y episodios recibidos por el ejecutivo; consultas de detalle usan los dos endpoints de cartera/ conversación arriba.
- Los registros de ingesta de leads existentes se buscan mediante la consulta del ledger `lead_ingest_events` en `leads_queries.py`. Los documentos CRM se consultan desde `leads`; el ledger de ingesta es `lead_ingest_events`. Los valores por defecto observados en `Config` para el universo de propiedades y captación son `universo_cartera_prop360` y `propiedades_captacion`, respectivamente, pero las variables de entorno pueden sobrescribirlos. El contrato safe de configuración se reproduce en `control-audit-snapshot/source/configuration/config_control_contract.py.txt`; no se copió el archivo de configuración completo.
- La columna de fecha de ingreso no equivale necesariamente a la fecha factual de consulta de Prop360. El dashboard y las consultas distinguen `created_at`, eventos de ingesta y actividad conversacional; reactivaciones y mensajes no deben asumirse como nuevos leads sin la lógica de deduplicación y atribución correspondiente.

### Captación

- `captacion_goals.py` entrega los indicadores de cobertura/actividad del módulo y usa la colección de captación obtenida mediante `Config.get_captacion_collection(db)`.
- `captacion_management.py`, `captacion_workforce.py` y `captacion_contact_identity.py` definen la población, asignación/estado de gestión, normalización de identidad y resultados CRM de captación.
- El endpoint recibe `executive`, `period_start`, `period_end`; el código combina métricas del período con ciertos estados actuales/backlog. Revisar cada campo: un pendiente actual no es necesariamente una actividad creada en el intervalo.
- Los cuatro bloques de resumen de Captación usan cobertura, contactabilidad, resultado de gestión e intermediación por portal; el HTML determina su jerarquía y visualización.

## Asignación, temperatura y SLA productivo

La instantánea incluye `chatbot/crm_assignment_cycle_gate.py`, el conjunto `chatbot/crm_sla_reassignment_*.py`, `crm_sla_hybrid_rescue.py`, `crm_sla_hybrid_stabilization.py`, `crm_sla_policy_freeze.py`, `crm_management.py`, `crm_metrics.py`, `business_calendar.py`, `lead_temperature.py` y `storage.py`.

Estos archivos permiten seguir el contrato de ciclos, temperatura, evaluación, protección/aborto y transferencia. `crm_sla_reassignment_executor.py`, transacción, worker, modelos, integración, runtime y cutover representan partes distintas del proceso; no tratar un vencimiento como prueba de transferencia. Para saber si se ejecutó una reasignación hay que seguir el registro de transferencia/resultado del ejecutor, no inferirlo del estado SLA solamente. La instantánea no cambia las reglas productivas.

## Pruebas incluidas

Se copiaron pruebas focalizadas existentes para tarjetas, períodos, alias/personas, cartera/demanda, compromisos, SLA/gestión, worker/transacción/ejecutor de reasignación, captación, filtros ejecutivos y resolución de propiedad. Rutas: `control-audit-snapshot/source/tests/`. Son fixtures de prueba, no datos de producción. No se ejecutaron para publicar esta instantánea: el objetivo es empaquetar el estado actual para auditoría, no certificarlo.

## Dependencias externas y límites de verificación

- La ejecución real depende de `config.Config`, autenticación/sesión, `chatbot.storage.get_db`, los nombres de colección inyectados por entorno, MongoDB y dependencias Python del proyecto (entre ellas FastAPI, PyMongo/BSON, Jinja2 y utilidades de fechas). Se excluyeron `config.py`, `.env*`, credenciales, claves y valores de configuración; los endpoints muestran qué parámetros consumen, pero la auditoría no puede reproducir consultas reales sin un entorno autorizado.
- No se incorporaron documentos de MongoDB, snapshots de clientes, conversaciones, registros CRM ni colecciones de captación. Los datos vivos no forman parte de esta rama.
- No se incluyeron componentes del chatbot/RAG ajenos a los imports/contratos de Control, la aplicación completa duplicada, archivos de respaldo, logs, previews de datos, `.codex` ni artefactos temporales.
- Los nombres de ejecutivos presentes en el código/fixtures se reemplazaron por etiquetas `Executive A`…`Executive E`; el nombre del titular de oficina y del usuario responsable de control se pseudonimizaron. Emails, teléfonos y ObjectIds que aparecían en fixtures se sustituyeron por valores sintéticos. Por eso las pruebas de esta copia sirven para inspeccionar la estructura y el contrato, pero los alias y fixtures de identidad no reproducen datos reales.
- Se excluyó `webhook.py` completo (módulo operativo extenso) y se añadió un extracto literal de las rutas Control. Esto evita publicar handlers y datos de otras funciones no requeridos para seguir la ruta analítica.
- La política de configuración de feriados, zonas horarias, horarios, umbrales y exclusiones se puede trazar en los módulos copiados, pero su activación efectiva depende del entorno/estado MongoDB no incluido. No hay evidencia de datos de producción en este paquete.

## Archivos de referencia incluidos

La instantánea contiene frontend, servicio analítico, consultas, configuración temporal compartida de períodos, identidad e historial de propiedad, módulo de captación, contratos/motores CRM y SLA directamente relevantes, extracto de endpoints y pruebas focalizadas. El listado completo se puede obtener enumerando `control-audit-snapshot/source/`.

## Confirmación de cambios

Esta publicación agrega documentación y una carpeta de instantánea auditada. No modifica el código de runtime en la raíz de la rama; no altera `main`; no cambia indicadores, reglas de asignación, notificaciones ni SLA; no escribe en MongoDB Atlas; no realiza despliegues.