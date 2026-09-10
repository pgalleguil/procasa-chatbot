# Arquitectura Owner Portal V3

## Estado y alcance

Documento de arquitectura futura basado en una auditoría técnica de solo lectura realizada el 10-09-2026. No implementa la siguiente fase.

- Cambios UI: 0.
- Escrituras MongoDB: 0.
- Cambios de código operativo, autenticación, emails, tokens, autorizaciones, eventos o ML: 0.
- No se hizo push ni despliegue.
- La colección operativa vigente para V3 es `universo_cartera_prop360`.
- `universo_cartera` se conserva únicamente como evidencia histórica de la campaña antigua; no es fuente primaria de la arquitectura nueva.

## Decisión de fuente de verdad

La propiedad que el propietario ve y la que el portal debe identificar parten de `universo_cartera_prop360`. La arquitectura debe evitar mezclarla silenciosamente con la cartera histórica.

| Dominio | Fuente | Observación auditada | Uso V3 |
|---|---|---|---|
| Propiedad vigente | `universo_cartera_prop360` | 1.965 documentos; 459 de `PROCASA SUCRE` | Fuente maestra primaria |
| Historia de cartera | `universo_cartera_prop360_historial` | 683 eventos: 480 cambios de precio, 83 de ejecutivo, 66 altas, 46 bajas, 7 actualizaciones y 1 reactivación | Fuente primaria de eventos Prop360 |
| Historia embebida | `universo_cartera_prop360.historial_cambios` | Presente en 1.963 documentos; los eventos de precio son minoritarios frente a los eventos de versión | Complemento, no sustituto de la colección histórica |
| Snapshot de pricing | `pricing_intelligence_property_snapshots_v1` | 1.965 documentos, una sola fecha de snapshot: 10-09-2026 | Lectura descriptiva actual; no es panel histórico |
| Mercado comunal | `mercado_comunal` | 123 combinaciones comuna/tipo; deriva de PDFs de análisis comunal | Contexto agregado, con fecha y hash de origen |
| Interés y leads | `leads` | 1.232 registros; vinculación estricta: 815 canónica, 13 alias, 403 no vinculados y 1 conflicto | Demanda observada, con límites de cobertura |
| Envíos históricos | `ajuste_precio` | 736 envíos registrados en las olas del 20 y 29-05-2026 | Auditoría de campaña, no fuente de precio vigente |
| Respuestas históricas | `price_updates` | 162 respuestas entre 01-12-2025 y 03-06-2026 | Outcomes descriptivos de campaña |
| Tasaciones | `tasaciones` / `tasaciones_final` | 428 / 2.604 documentos | Evidencia de tasación si existe y si su fecha es válida |
| Cartera histórica | `universo_cartera` | 2.911 documentos, usada por el pipeline legado | Solo comparación y trazabilidad histórica |

Regla explícita: si una propiedad existe en ambas carteras, el portal V3 usa el estado y el identificador de `universo_cartera_prop360`. Los datos de `universo_cartera` no deben rellenar campos actuales salvo que se presenten como histórico, con fuente, fecha y advertencia.

## Lineaje de datos

### Camino operativo futuro

```text
universo_cartera_prop360
        ├── identidad, estado, precio y atributos actuales
        ├── historial_cambios / universo_cartera_prop360_historial
        ├── leads (vinculación canónica o alias verificado)
        └── pricing_intelligence_property_snapshots_v1
                  └── API Owner Portal → vista descriptiva del propietario

mercado_comunal ────────────────────────────┘ contexto comunal agregado
tasaciones ─────────────────────────────────┘ evidencia de valoración, si aplica
```

### Camino legado auditado

```text
PDFs locales de análisis comunal / tasaciones
        └── scripts históricos de ingestión
                  ├── mercado_comunal
                  └── tasaciones

universo_cartera + propiedades_accionables
        └── send_baja_precio.py
                  ├── previews XLSX/CSV
                  ├── ajuste_precio (envío)
                  └── price_updates (respuesta)
```

El repositorio no contiene un generador de los PDFs comunales ni de las tasaciones. `mercado_comunal_pipeline.py` y `tasaciones_pipeline.py` leen PDFs ya existentes y persisten datos derivados. El envío histórico seleccionaba y adjuntaba PDFs físicos desde `C:\Users\pgall\Desktop\Analisis Comercial 2` y `C:\Users\pgall\Desktop\Tasaciones`.

## Componentes propuestos para V3

| ID | Componente | Responsabilidad | Estado de datos |
|---|---|---|---|
| 01 | Source Registry | Registrar colección, fecha, hash, granularidad y reglas de uso de cada dato | Parcial: existe metadata en algunas fuentes, falta contrato único |
| 02 | Property Snapshot | Congelar identidad, estado, precio y atributos de la propiedad actual | Parcial: snapshot actual disponible; falta serie temporal diaria |
| 03 | Market Context | Entregar comuna/tipo, publicaciones, actividad, UF/m² y señales de mercado | Parcial: `mercado_comunal` está agregado y deriva de PDFs |
| 04 | Lead Linkage | Asociar interés a `property_code` solo con identificadores verificados | Parcial: 828 links estrictos en el universo de leads; 403 leads quedan sin match |
| 05 | Price Event Normalizer | Normalizar cambios de precio con timestamp con zona horaria, moneda y fuente | Parcial: 480 eventos Prop360; faltan series completas y cobertura histórica |
| 06 | Descriptive Simulator V1 | Comparar escenarios de precio contra cohortes observadas | Viable con advertencias; no atribuye causalidad |
| 07 | Property-by-Week Panel | Formar la unidad analítica `property_code × week` | Bloqueado para histórico; faltan snapshots y exposición consistentes |
| 08 | ML Boundary | Separar demanda futura de efecto causal del precio | Bloqueado; no entrenar con el estado actual |
| 09 | Owner Portal API | Exponer DTOs tipados, separados por evidencia, estimación y recomendación | Parcial: lectura actual existente; falta contrato V3 cerrado |
| 10 | Instrumentation | Registrar apertura, secciones vistas, simulador, autorización y contacto | Bloqueado hasta definir consentimiento y persistencia de eventos |
| 11 | Auth/Security | Acceso autorizado por propietario, propiedad y sesión | Congelado: no modificar autenticación, tokens ni autorizaciones en esta fase |
| 12 | QA/Observability | Validar freshness, cobertura, lineage, latencia y coherencia | Parcial: hay pruebas del pricing intelligence; falta QA de panel futuro |

## Contratos de información

La API V3 debe separar tres capas para no presentar una heurística como una predicción:

1. `observed`: precio actual, atributos, publicaciones, leads vinculados, eventos de precio y métricas comunales con fuente y fecha.
2. `estimated`: percentiles o rangos calculados desde comparables observados; siempre con tamaño de muestra, ventana y nivel de cobertura.
3. `recommended`: recomendación de negocio futura, solo si existe modelo validado, incertidumbre, regla de abstención y revisión humana.

Cada bloque debe incluir, como mínimo, `property_code`, `source`, `observed_at`, `valid_from`, `valid_to` cuando corresponda, `currency`, `data_quality` y `limitations`. Un dato no debe viajar como vigente si solo proviene de la cartera histórica.

Reglas temporales:

- Los eventos usados analíticamente deben tener timestamp explícitamente timezone-aware.
- Las fechas naïve de `universo_cartera.historial_cambios` sirven para diagnóstico histórico, no para un cálculo productivo de antes/después.
- La ventana post-cambio se considera censurada si no hay 30 días observables completos.
- Las métricas de leads deben separar enlace canónico, alias verificado, conflicto y no-match.
- Una tasación o PDF comunal nunca debe convertirse automáticamente en precio autorizado.

## Simulador V1 descriptivo

El simulador futuro puede existir como cálculo descriptivo, sin cambiar precio ni enviar email. Para una propiedad y una fecha de corte recibe:

- precio vigente y moneda desde `universo_cartera_prop360`;
- comuna, tipo, superficie y demás atributos disponibles;
- cohorte comparable comuna × tipo, con filtros explícitos y tamaño de muestra;
- leads vinculados y su ventana de observación;
- cambios de precio Prop360 con timestamp válido;
- contexto agregado de `mercado_comunal`, mostrando fecha del reporte y antigüedad.

Para cada escenario produce delta absoluto/porcentual, posición o percentil dentro de la cohorte, rango observado y flags de calidad. No afirma que el cambio produzca más leads, no estima probabilidad de cierre y no recomienda una rebaja automática. La salida debe abstenerse cuando la cohorte sea pequeña, la moneda no sea comparable, el precio esté incompleto o la ventana temporal esté censurada.

## Arquitectura ML V2 futura

Son dos problemas distintos y deben permanecer separados:

### Modelo 1: pronóstico de demanda

Objetivo posible: leads verificados o consultas por propiedad en una ventana futura. Requiere panel histórico `property_code × week`, exposición, disponibilidad, publicaciones, cambios de ejecutivo, estacionalidad, precio y definición de target. Debe evaluarse con cortes temporales, baseline simple, métricas por cohorte y predicción con incertidumbre.

### Modelo 2: efecto causal del precio

Objetivo posible: diferencia contrafactual de demanda o tiempo a cierre bajo un precio alternativo. Requiere tratamiento claramente definido, grupo de comparación, confusores observados, soporte común, controles por disponibilidad/exposición y resultado confiable. Un antes/después simple no identifica causalidad.

Ambos modelos necesitarán versionado, partición temporal, monitoreo de drift, auditoría de sesgos, abstención automática y aprobación humana. En el estado actual no se entrena ninguno: hay un solo `snapshot_run` persistido para 10-09-2026, no hay panel diario histórico consolidado, no hay target de cierre confiable y no hay exposición completa del portal.

## Estado de viabilidad

| Nivel | Definición | Veredicto actual | Motivo |
|---|---|---|---|
| A | Descriptivo: estado, comparables, percentiles y eventos observados | VIABLE CON LIMITACIONES | Fuente actual disponible; 19 caídas de precio válidas en 11 propiedades SUCRE, pero profundidad histórica limitada |
| B | Cuasi-experimental: antes/después con comparación | PARCIAL / NO LISTO | No hay panel estable ni exposición completa; las caídas actuales tienen ventana post de 30 días censurada |
| C | Predictivo/causal validado | NO VIABLE AHORA | Falta panel longitudinal, targets, tratamiento/control y observabilidad del portal |

## Hallazgos de calidad que afectan V3

- `universo_cartera_prop360` contiene 1.963 documentos con `historial_cambios`, pero solo una fracción tiene eventos de precio; no debe asumirse que ausencia de evento significa ausencia de cambio.
- En `PROCASA SUCRE` hay 459 propiedades actuales y 32 entradas de precio detectadas en historia embebida; 19 son caídas significativas de al menos 0,5% en 11 propiedades.
- Los 480 eventos de `universo_cartera_prop360_historial` son la fuente temporal preferente para cambios Prop360.
- La corrida de snapshots tiene 1.965 documentos y una sola fecha; los leads persistidos en el snapshot no aportan todavía una serie `previous_30d` útil para inferencia.
- `mercado_comunal` valida cifras agregadas de los PDFs, pero sus gráficos se parsean heurísticamente y no constituyen transacciones fila a fila.
- `leads` tiene 403 registros sin enlace estricto y 1 conflicto; cualquier porcentaje debe informar denominador y cobertura.
- Los resultados de campaña deben contar respuestas en `price_updates` y envíos en `ajuste_precio`; los estados legacy de `contactos` pueden duplicarse o tener fechas de email engañosas.

## Backlog previo a cualquier implementación

1. Cerrar un diccionario de datos con propietario, fuente, fecha, moneda, unidad, frescura y retención.
2. Persistir snapshots diarios o semanales de `universo_cartera_prop360` con versionado inmutable.
3. Completar `property_code × week` con exposición, disponibilidad, precio, leads y resultados.
4. Resolver y auditar los 403 leads sin match y el conflicto sin relajar el enlace estricto.
5. Formalizar el contrato de eventos del portal y su consentimiento antes de instrumentar.
6. Definir outcomes: lead verificado, visita agendada, visita realizada, oferta, mandato y cierre.
7. Diseñar QA de cohortes y ventanas censuradas para el simulador.
8. Recién después, evaluar modelos separados de demanda y efecto causal.

## No objetivos de esta fase

No se agrega un panel, no se cambia el Owner Portal desplegado, no se reescribe MongoDB, no se crean índices, no se modifican Render, autenticación, emails, tokens, autorizaciones, eventos ni ML. Esta arquitectura queda como base para revisión humana y nuevas instrucciones.
