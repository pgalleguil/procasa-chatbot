# Dashboard Comercial PROCASA — validación correctiva V3

Fecha: 2026-07-22
Commit base visual: `3f243e10128de7b9b74fdc2df5ed6275edbad925`
Versión rechazada: `bf4a7b75d5b30fbc6ad1464fd9085e0dc30a1a64`

## Evidencia visual

- `reports/commercial-dashboard/before-original-1440.png`
- `reports/commercial-dashboard/rejected-v2-1440.png`
- `reports/commercial-dashboard/corrected-v3-1440.png`
- `reports/commercial-dashboard/corrected-v3-390.png`

Las capturas fueron generadas con Chromium/Playwright y la consulta canónica. La versión corregida también se verificó realmente a 1366×768, 1024×768, 390×844 y 360×800.

## Paridad canónica

Consulta:

`period_start=2026-07-21&period_end=2026-07-21&compare=prev&period_preset=today`

| Campo | Base | V3 | Resultado |
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

Los cambios son de presentación, acceso interno y eliminación del ticker. No se alteraron las consultas ni las definiciones canónicas.

## Validación responsive local previa al despliegue

| Viewport | Overflow general | Errores de consola | Ticker |
|---|---|---|---|
| 1440×900 | No | 0 | Ausente |
| 1366×768 | No | 0 | Ausente |
| 1024×768 | No | 0 | Ausente |
| 390×844 | No | 0 | Ausente |
| 360×800 | No | 0 | Ausente |

## Cambios correctivos

- Se eliminó completamente el ticker macroeconómico, su endpoint, parser, caché y carga asíncrona.
- Se redujo el resumen a seis KPI primarios.
- Se recuperó una composición sobria: cabecera compacta, filtros secundarios en drawer, secciones espaciadas y superficies sin decoración excesiva.
- Las comparaciones de KPI muestran la fecha real del periodo anterior.
- La vista interna utiliza los nombres reales retornados por el backend.
- Se conservaron cancelación de solicitudes, caché del frontend, actualización no bloqueante y consultas analíticas paralelas.

## Limitaciones funcionales reales

- Seguimientos vencidos, estancamiento y resultado faltante permanecen S/I cuando el contrato no aporta evidencia.
- Las propiedades sin leads no forman parte del payload actual; no se infiere demanda nula.
- Dormitorios, baños, superficie y estacionamientos permanecen S/I si su cobertura no es demostrable.
