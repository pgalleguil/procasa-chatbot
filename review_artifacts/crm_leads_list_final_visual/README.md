# Evidencia final — listado de Gestión de Leads

Capturas generadas desde `/crm-leads-review?view=list` con Playwright controlado, DPR 1 y `fullPage=false`.

- Branch: `agent/crm-leads-list-final`
- Commit funcional: `f08fc7c`
- Generado: `2026-08-21 11:59:30 -04:00`
- Datos: fixture demo local; sin MongoDB ni escrituras externas.
- Detalle, modal interno, backend, SLA, alarmas y WhatsApp: congelados.

## Capturas

- `01-list-final-1440.png` — listado desktop 1440×1000.
- `02-list-final-390.png` — listado mobile 390×844.
- `03-list-final-390-scroll.png` — listado mobile desplazado 620 px, 390×844.
- `04-list-final-390-modal.png` — listado mobile con modal canónico, 390×844.

## QA visual/DOM

- Desktop: 6 columnas, 9 leads visibles, sin overflow.
- Mobile: una superficie uniforme por card, prioridad y enviado en la fila superior, ejecutivo y CTA en el footer.
- Card mobile medida: 156,9 px.
- Card mobile: 346 px de ancho útil, sobre el 94% del main interior.
- CTA mobile: 116×42 px, completamente dentro de la card.
- Modal canónico: abre en desktop y mobile.

Estas imágenes documentan el estado implementado y medido; no constituyen una declaración de aprobación visual.
