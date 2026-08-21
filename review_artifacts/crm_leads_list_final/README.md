# Evidencia final — listado de Gestión de Leads

Capturas generadas desde la aplicación local real en `/crm-leads-review?view=list`.

- Rama: `agent/crm-leads-list-final`
- Commit funcional: `fd742b8`
- Generado: `2026-08-21 11:21:17 -04:00`
- Datos: fixture demo local; sin MongoDB, escrituras externas ni datos productivos.
- Modal: el mismo modal canónico existente, abierto desde `Gestionar`.
- Detalle, propietario, administración, SLA, alarmas, WhatsApp y rotación: fuera de alcance y sin cambios.

## Capturas

1. `01-list-only-desktop.png` — listado, viewport solicitado 1440×1000.
2. `02-list-only-desktop-modal.png` — listado + modal canónico, viewport solicitado 1440×1000.
3. `03-list-only-mobile.png` — listado móvil, viewport solicitado 390×844.
4. `04-list-only-mobile-modal.png` — listado móvil + modal canónico, viewport solicitado 390×844.
5. `05-list-only-desktop-1024.png` — listado, viewport solicitado 1024×768.

La búsqueda visual conserva el contrato actual: `busqueda` para nombre/teléfono y `property_code` como filtro separado, porque el backend no expone un único parámetro para los tres tipos.

Las imágenes documentan el estado implementado y medido; no constituyen una declaración de aprobación visual.
