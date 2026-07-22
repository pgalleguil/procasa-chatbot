# Dashboard Comercial PROCASA — validación Premium V2

Fecha: 2026-07-22  
Rama de trabajo: `feature/commercial-dashboard-premium-v2`  
Commit base: `3f243e10128de7b9b74fdc2df5ed6275edbad925`

## Resguardo

- Rama: `backup/commercial-dashboard-pre-premium-20260722`
- Tag: `commercial-dashboard-pre-premium-2026-07-22`
- La rama y el tag apuntan al commit base y se publicaron antes de modificar archivos.

## Paridad canónica

Consulta validada:

`period_start=2026-07-21&period_end=2026-07-21&compare=prev&period_preset=today`

| Campo | Antes | Premium V2 | Resultado |
|---|---:|---:|---|
| Periodo actual | 2026-07-21 | 2026-07-21 | Igual |
| Periodo comparable | 2026-07-20 | 2026-07-20 | Igual |
| Unidad | `lead._id` | `lead._id` | Igual |
| Leads recibidos | 5 | 5 | Igual |
| Hot actuales | 2 | 2 | Igual |
| Intención de visita | 2 | 2 | Igual |
| Visitas coordinadas | 0 | 0 | Igual |
| Cierres | 0 | 0 | Igual |
| SLA | 100,0 % | 100,0 % | Igual |
| Temperatura histórica | S/I | S/I | Igual |
| Cobertura temperatura histórica | 0 % | 0 % | Igual |

El modo sin datos (`1990-01-01`) conserva SLA como `null`/S/I; no lo convierte en cero.

## Rendimiento medido

Medición local contra la misma base y consulta, sin modificar definiciones:

| Medición | Antes | Premium V2 |
|---|---:|---:|
| API consolidada, caché fría | 2.756 ms | 1.668 ms |
| API consolidada, caché caliente | no registrado de forma comparable | 25 ms |
| Solicitudes principales iniciales | 3 | 3 |
| Solicitudes duplicadas del dashboard | 0 observadas | 0 observadas |

La reducción fría proviene de ejecutar en paralelo agregaciones MongoDB independientes y de solo lectura. El frontend añade caché por combinación de filtros, `AbortController`, protección contra respuestas fuera de orden y conservación del último resultado válido.

## Estados y accesibilidad

- Skeleton inicial de KPI y gráficos sin vaciar la estructura.
- Estado de actualización con datos anteriores visibles.
- Barra superior de progreso y `aria-busy`.
- Error parcial con último resultado válido y botón Reintentar.
- Estado vacío, S/I y sin cobertura diferenciados.
- Drawer de filtros en escritorio y bottom sheet móvil.
- Navegación por teclado en tabs, filas de ejecutivos y orden de tablas.
- `aria-expanded`, `aria-selected`, `aria-live` y foco visible.
- `prefers-reduced-motion` desactiva transiciones y ticker.

## Contexto macroeconómico

Se muestran únicamente UF, USD/CLP y TPM desde la página oficial de indicadores diarios del Banco Central de Chile. La consulta es diferida, tiene caché horaria y falla de forma cerrada: si la fuente no responde, la franja se oculta. IPC anual y tasa hipotecaria se omiten hasta disponer de una interfaz oficial estable y comprobable.

## Privacidad

La vista pública funciona en modo portafolio: reemplaza identidades por alias deterministas y omite el selector nominal. Usuarios admin/supervisor autenticados conservan la vista interna. No se exponen teléfonos, correos, RUT ni identificadores personales.

## Limitaciones explícitas

- Seguimientos vencidos, estancamiento y resultado faltante se muestran S/I cuando el contrato actual no entrega evidencia suficiente.
- Las propiedades sin leads no forman parte del payload actual; por ello no se afirma demanda nula.
- Dormitorios, baños, superficie y estacionamientos permanecen S/I por falta de cobertura demostrable.
- La tasa hipotecaria y el IPC anual no se muestran sin fuente oficial estable disponible para consumo automático.
