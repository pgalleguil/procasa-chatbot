# Fase 1-L.1 — Integridad de viewport y ancho

Validación ejecutada contra `/crm-leads-review?view=list` con Playwright controlado, `deviceScaleFactor=1`, sin emulación adicional y `fullPage=false`.

## Causa encontrada

La limitación no estaba en el CSS/template del listado ni en un wrapper de review. El navegador embebido usado para las capturas anteriores ignoró la equivalencia solicitada entre viewport CSS y PNG físico: al pedir 1440×1000 reportó `innerWidth=1694` y `devicePixelRatio=2.125`. Por eso los PNG anteriores quedaron con dimensiones físicas distintas.

La geometría CSS real ya utilizaba el ancho disponible:

- `.main-content`: `margin-left: 80px`, `padding: 30px`, ancho automático.
- `#crmDynamicContent`: ocupa el ancho interior del main.
- `.table-container`: ocupa el 100% del listado, con padding interno de 24 px desktop / 14 px mobile.
- En mobile, `.sidebar` cerrada queda fuera de pantalla y `.main-content` usa `margin-left: 0`.

No se modificó el CSS visual del listado para simular escala. La corrección fue de contexto de captura y evidencia.

## Capturas anteriores

| Archivo | Viewport solicitado | Tamaño físico real | DPR del runtime anterior |
|---|---:|---:|---:|
| `01-list-only-desktop.png` | 1440×1000 | 1993×1384 | 2.125 |
| `02-list-only-desktop-modal.png` | 1440×1000 | 1993×1384 | 2.125 |
| `03-list-only-mobile.png` | 390×844 | 518×1167 | 2.125 |
| `04-list-only-mobile-modal.png` | 390×844 | 539×1167 | 2.125 |
| `05-list-only-desktop-1024.png` | 1024×768 | 1395×1062 | 2.125 |

## Validación nueva

| Viewport | innerWidth | DPR | Sidebar cerrada | Main | List container | Ancho disponible usado | scrollWidth |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1440×1000 | 1440 | 1 | 80 px | 1360 px | 1300 px | 100% | 1440 |
| 1024×768 | 1024 | 1 | 80 px | 944 px | 884 px | 100% | 1024 |
| 390×844 | 390 | 1 | 0 px efectivos | 390 px | 366 px | 100% del main interior | 390 |

En mobile, la sidebar abierta mide 280 px después de completar la transición; el main permanece en 390 px porque la navegación es overlay y no roba ancho al contenido.

## Escala

- CSS `zoom`: `1`; no se detectó `zoom` distinto en el árbol del listado.
- `transform: scale(...)` en ancestors del listado: no se detectó.
- DPR nuevo: `1`.
- Meta viewport: `width=device-width, initial-scale=1.0`.
- Capturas nuevas: `fullPage=false`.

Las assertions y las métricas completas quedan en `qa_width_playwright.py` y `metrics.json`.
