# Arquitectura de información Owner Portal V3

## Propósito y límites

Esta es una especificación conceptual del panel futuro para propietarios. No es una iteración visual ni una orden de implementación.

- Cambios UI: 0.
- La fuente primaria actual es `universo_cartera_prop360`.
- La colección `universo_cartera` solo aparece como histórico de la campaña antigua.
- Cada bloque debe mostrar origen, fecha de observación, cobertura y limitaciones.
- Las secciones de predicción y recomendación permanecen ocultas/bloqueadas hasta validación metodológica.

## Orden conceptual del panel

| Orden | Sección | Pregunta del propietario | Disponibilidad actual | Fuente principal |
|---|---|---|---|---|
| 01 | Identidad y estado | ¿Qué propiedad estoy viendo y cuál es su estado? | READY | `universo_cartera_prop360` |
| 02 | Precio y posicionamiento | ¿Cuál es mi precio vigente y cómo se posiciona? | READY/PARTIAL | Cartera vigente + cohortes observadas |
| 03 | Mercado y comparables | ¿Qué ocurre en mi comuna y tipo de propiedad? | PARTIAL | `mercado_comunal` |
| 04 | Demanda e interés | ¿Cuánto interés verificable ha existido? | PARTIAL | `leads` con enlace estricto |
| 05 | Historial de precio | ¿Qué cambios de precio están documentados? | PARTIAL | `universo_cartera_prop360_historial` + historia embebida |
| 06 | Simulador V1 descriptivo | ¿Qué cambia si pruebo otro precio dentro de lo observado? | PARTIAL | Precio actual + cohorte + eventos válidos |
| 07 | Publicaciones | ¿Dónde se está mostrando mi propiedad? | PARTIAL | Publicaciones y atributos de cartera, según cobertura |
| 08 | Contacto y siguiente paso | ¿Cómo converso con mi ejecutivo? | READY/PARTIAL | Ejecutivo vigente y autorización existente |
| 09 | Metodología y calidad | ¿De dónde salen los números y qué tan completos son? | READY como contenido; datos PARTIAL | Registro de fuente y flags de calidad |
| 10 | Pronóstico/recomendación | ¿Qué pasará o qué precio debo aceptar? | BLOCKED | Requiere V2, validación humana y autorización explícita |

### Reglas de lectura

La sección 02 no debe presentar un percentil como tasación. La sección 03 debe distinguir el reporte comunal agregado de una transacción individual. La sección 04 debe mostrar “interés vinculado” y no “demanda total” cuando existan no-match. La sección 05 debe distinguir cambio de precio observado, fecha de captura y periodo sin observación. La sección 06 debe usar lenguaje de escenario descriptivo, nunca de promesa.

## Panel conceptual `property_code × week`

La unidad analítica futura es una fila por propiedad y semana, no una fila por email ni por evento aislado.

| Campo | Contenido mínimo | Regla |
|---|---|---|
| `property_code` | Identificador canónico | Obligatorio; no sustituir por dirección difusa |
| `week_start` | Inicio de semana en zona horaria definida | Obligatorio y timezone-aware |
| `active_flag` | Disponible/publicable en la semana | Desde estado vigente congelado |
| `price_uf`, `price_clp`, `currency` | Precio publicado | Moneda y conversión deben ser explícitas |
| `price_change_flag`, `price_change_pct` | Cambio observado en la semana | Solo con evento y fecha válidos |
| `executive_id` / oficina | Responsable vigente | Versionado para no mezclar gestiones |
| atributos | comuna, tipo, superficie, dormitorios, baños | Snapshot de la semana |
| `publication_count` / exposición | Publicaciones y exposición por canal | Requiere instrumentación o fuente confiable |
| `verified_leads` | Leads vinculados | Separar canónico, alias, conflicto y no-match |
| `visits`, `offers`, `mandates`, `closed` | Outcomes comerciales | Definir evento y fecha de cada outcome |
| `market_context_id` | Referencia al contexto comuna × tipo | Incluye fecha y calidad del PDF/dato |
| `data_quality` | Cobertura, freshness, censura y flags | Obligatorio para modelado |

Este panel todavía no existe como serie histórica completa. La información actual permite una primera vista descriptiva, pero no un análisis causal ni un pronóstico confiable.

## Simulador V1, solo descriptivo

El flujo conceptual es:

1. Seleccionar `property_code` y congelar el snapshot actual.
2. Mostrar precio y moneda vigentes.
3. Construir cohorte por comuna × tipo y filtros de comparabilidad.
4. Probar escenarios de precio definidos por el usuario dentro del rango observado.
5. Mostrar delta, posición/percentil, tamaño de cohorte y fecha de los datos.
6. Exponer advertencias si faltan superficie, moneda, eventos, leads o ventana completa.
7. Ofrecer contacto con el ejecutivo; nunca autorizar ni mutar el precio desde el cálculo descriptivo.

La salida no debe decir “venderás más rápido”, “obtendrás X leads” ni “el precio óptimo es”. Es una comparación contra evidencia observada, con abstención si la muestra o la calidad no alcanzan.

## Journey futuro del propietario

```text
Enlace/acceso autorizado
        ↓
Confirmar identidad y propiedad
        ↓
Entender estado actual y precio
        ↓
Revisar mercado, comparables e interés verificable
        ↓
Consultar historia y probar escenario descriptivo
        ↓
Leer metodología, fecha y limitaciones
        ↓
Contactar al ejecutivo / solicitar conversación
        ↓
Autorización explícita y trazable (solo cuando exista el flujo aprobado)
```

El objetivo es que el propietario entienda antes de actuar. El portal no debe convertir una visita al informe en consentimiento de rebaja, ni usar un token público como autorización.

## Benchmark conceptual de Owner Portal

Los referentes se utilizan como patrones de producto para comparar capacidades, no como copia ni como validación contractual de sus implementaciones.

| Patrón | Referente internacional | Aplicación futura PROCASA |
|---|---|---|
| Contexto de valor y posición | Zillow / Redfin | Explicar precio, comparables y fecha sin confundirlo con tasación |
| Actividad de publicaciones | Compass / portales de listados | Resumir canales y frescura de publicación |
| Lectura de mercado local | Rightmove / Zoopla | Traducir comuna × tipo a un resumen comprensible |
| Escenarios transparentes | Flujos de pricing de plataformas digitales | Simulador descriptivo con rango, muestra y abstención |
| Siguiente acción humana | Portales de brokerage con contacto de agente | Conectar al ejecutivo sin autorización automática |
| Confianza y explicación | Patrones de dashboards financieros | Mostrar fuente, fecha, cobertura, limitaciones y trazabilidad |

## Matriz de valor y disponibilidad

| Capacidad | Valor para PROCASA | Disponibilidad actual | Fuente potencial/actual | Acción futura |
|---|---|---|---|---|
| Identidad de propiedad | Evita errores de inmueble y mejora confianza | Disponible | `universo_cartera_prop360` | Congelar DTO con `property_code` |
| Precio vigente | Base común para conversación comercial | Disponible en la mayoría; cobertura parcial por operación | Cartera Prop360 | Normalizar moneda y freshness |
| Mercado comuna × tipo | Da contexto defendible al ejecutivo y propietario | Parcial | `mercado_comunal`, PDFs | Versionar reportes y calidad |
| Comparables observados | Permite posicionamiento descriptivo | Parcial | Cartera vigente + atributos | Definir cohortes y filtros |
| Demanda/leads | Prioriza seguimiento y muestra interés verificable | Parcial | `leads` | Resolver no-match y definir lead válido |
| Distribución de publicaciones | Identifica cobertura y posibles brechas de exposición | Parcial | Cartera/canales; falta exposición real | Instrumentar impresiones y vigencia |
| Historial de precio | Hace visible la trayectoria | Parcial | Historial Prop360 | Capturar snapshots completos y fechas válidas |
| Outcomes de campaña | Mide aceptación, contacto y no disponibilidad | Histórico, no vigente | `ajuste_precio`, `price_updates` | Separar campañas y cohortes con lineage |
| Intención del propietario | Permite adaptar el siguiente paso | No disponible de forma completa | Evento explícito futuro | Diseñar consentimiento y estado |
| Efecto del cambio de precio | Ayuda a evaluar decisiones | No listo | Panel temporal + exposición + outcomes | Diseñar cuasi-experimento; no inferir con antes/después simple |
| Pronóstico de demanda | Planifica gestión y seguimiento | Bloqueado | Panel longitudinal futuro | Baseline, validación temporal y abstención |
| Efecto causal del precio | Informa contrafactual de negocio | Bloqueado | Tratamiento/control y confusores | Modelo separado, revisión metodológica |
| Visita realizada/oferta/cierre | Conecta portal con resultado económico | No disponible como target completo | CRM/eventos comerciales | Definir IDs, fechas y fuente de verdad |

## Metodología visible al propietario

Todo indicador debe poder responder “qué”, “de cuándo”, “de dónde” y “con qué cobertura”. El contenido mínimo es:

- fecha de corte y zona horaria;
- fuente y granularidad;
- tamaño de la cohorte;
- definición de lead y regla de vinculación;
- advertencia de datos censurados o faltantes;
- diferencia entre observado, estimado y recomendado;
- canal de contacto para resolver discrepancias.

## Estado de implementación

Este documento no cambia templates, rutas ni componentes. La URL de revisión humana y el commit desplegado permanecen congelados. Cualquier implementación posterior requiere nuevas instrucciones, revisión de datos y autorización separada.
