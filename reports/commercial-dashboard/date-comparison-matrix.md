# Matriz temporal del Dashboard Comercial

- Ancla: `2026-07-21T12:00:00-04:00`
- Zona horaria: `America/Santiago`
- Resultado: **20/20 PASS**

## Contratos

- Semana: lunes hasta la fecha ancla; `prev` desplaza siete días.
- Mes: día 1 hasta la fecha ancla; conserva duración en el mes previo y, si este es más corto, ajusta el inicio hacia atrás.
- Año anterior: mismas fechas calendario; 29 de febrero se ajusta al 28 de febrero.
- MongoDB: intervalo semiabierto desde el inicio civil local hasta el primer instante válido del día siguiente, convertido a UTC.

| Preset | Comparación | Actual | Comparable esperado | Comparable recibido | Estado |
|---|---|---|---|---|---|
| today | auto | 2026-07-21 → 2026-07-21 | 2026-07-14 → 2026-07-14 | 2026-07-14 → 2026-07-14 | PASS |
| today | prev | 2026-07-21 → 2026-07-21 | 2026-07-20 → 2026-07-20 | 2026-07-20 → 2026-07-20 | PASS |
| today | yoy | 2026-07-21 → 2026-07-21 | 2025-07-21 → 2025-07-21 | 2025-07-21 → 2025-07-21 | PASS |
| today | none | 2026-07-21 → 2026-07-21 | — → — | — → — | PASS |
| week | auto | 2026-07-20 → 2026-07-21 | 2026-07-13 → 2026-07-14 | 2026-07-13 → 2026-07-14 | PASS |
| week | prev | 2026-07-20 → 2026-07-21 | 2026-07-13 → 2026-07-14 | 2026-07-13 → 2026-07-14 | PASS |
| week | yoy | 2026-07-20 → 2026-07-21 | 2025-07-20 → 2025-07-21 | 2025-07-20 → 2025-07-21 | PASS |
| week | none | 2026-07-20 → 2026-07-21 | — → — | — → — | PASS |
| month | auto | 2026-07-01 → 2026-07-21 | 2026-06-01 → 2026-06-21 | 2026-06-01 → 2026-06-21 | PASS |
| month | prev | 2026-07-01 → 2026-07-21 | 2026-06-01 → 2026-06-21 | 2026-06-01 → 2026-06-21 | PASS |
| month | yoy | 2026-07-01 → 2026-07-21 | 2025-07-01 → 2025-07-21 | 2025-07-01 → 2025-07-21 | PASS |
| month | none | 2026-07-01 → 2026-07-21 | — → — | — → — | PASS |
| 30d | auto | 2026-06-22 → 2026-07-21 | 2026-05-23 → 2026-06-21 | 2026-05-23 → 2026-06-21 | PASS |
| 30d | prev | 2026-06-22 → 2026-07-21 | 2026-05-23 → 2026-06-21 | 2026-05-23 → 2026-06-21 | PASS |
| 30d | yoy | 2026-06-22 → 2026-07-21 | 2025-06-22 → 2025-07-21 | 2025-06-22 → 2025-07-21 | PASS |
| 30d | none | 2026-06-22 → 2026-07-21 | — → — | — → — | PASS |
| custom | auto | 2026-07-10 → 2026-07-15 | 2026-07-04 → 2026-07-09 | 2026-07-04 → 2026-07-09 | PASS |
| custom | prev | 2026-07-10 → 2026-07-15 | 2026-07-04 → 2026-07-09 | 2026-07-04 → 2026-07-09 | PASS |
| custom | yoy | 2026-07-10 → 2026-07-15 | 2025-07-10 → 2025-07-15 | 2025-07-10 → 2025-07-15 | PASS |
| custom | none | 2026-07-10 → 2026-07-15 | — → — | — → — | PASS |

Historial atrás/adelante: **PASS**
Errores de consola: **0**
