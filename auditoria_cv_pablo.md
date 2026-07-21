# Auditoría del CV de Pablo Galleguillos

## Alcance y criterio

Revisión realizada sobre el CV fuente `C:\Users\pgall\Downloads\CV - Pablo Galleguillos (1).docx`, el repositorio completo y una muestra de vacantes chilenas vigentes a julio de 2026. El CV fuente tiene cuatro páginas y está construido principalmente con una tabla de siete columnas. El diagnóstico separa la información declarada por el candidato, la evidencia del repositorio y las métricas que todavía requieren validación.

## Diagnóstico ejecutivo

El perfil tiene una propuesta de valor senior poco común: combina formación de Ingeniero Comercial, experiencia en control de gestión y finanzas, capacidad analítica, desarrollo de soluciones y aplicación de IA al ciclo comercial. El CV actual no transmite esa combinación. Se concentra en funciones genéricas, presenta la experiencia tecnológica como una lista extensa de herramientas y dedica espacio a datos personales que no aportan a selección.

La denominación profesional más defendible para la experiencia reciente es **Control de Gestión, Business Intelligence y Automatización**. Describe el trabajo realizado sin inventar un cargo contractual ni presentar el perfil como desarrollador de software puro.

## Fortalezas

- Experiencia transversal en servicios financieros, energía, retail e inmobiliaria.
- Base sólida de control de gestión: presupuesto, Capex/Opex, cierres, análisis de variaciones, KPIs, reportería y seguimiento comercial.
- Capacidad demostrable para traducir problemas de negocio en soluciones de datos y automatización.
- Desarrollo de una solución integral de inteligencia comercial inmobiliaria: CRM, captación, leads, chatbot, scraping, clasificación, RAG, alertas y reportería.
- Tecnologías implementadas en el repositorio: Python, FastAPI, MongoDB, APIs, HTML/CSS/JavaScript, Playwright, ETL, Excel automatizado, PDF, embeddings y modelos de lenguaje mediante API.
- Evidencia tangible de escala en artefactos del repositorio: reporte de 2.086 captaciones, libro de control con 426 registros de leads y 1.289 registros de captación, y 14.190 respaldos HTML distribuidos entre Yapo, TocToc y Prop360/Convecta.
- Formación reciente alineada con el reposicionamiento: Diplomado en Advanced Business Analytics, Universidad de Chile, en curso desde junio de 2026.

## Debilidades del CV actual

- **Posicionamiento difuso:** el resumen mezcla control de gestión, búsqueda de estabilidad y una lista técnica, pero no define el valor diferencial negocio-datos-tecnología.
- **Extensión excesiva:** cuatro páginas, con párrafos largos y repetición de ideas.
- **Baja compatibilidad ATS:** uso de una tabla compleja de siete columnas; fechas, empresas y funciones pueden perder su relación durante el parseo.
- **Experiencia reciente desactualizada:** el CV termina la experiencia en Procasa en noviembre de 2022 y no refleja los sistemas desarrollados posteriormente ni la situación laboral actual descrita por el candidato.
- **Logros no defendibles tal como están escritos:** expresiones como “aumento significativo”, “líder en captación” y “líder en unidades vendidas” no tienen una métrica o fuente incorporada.
- **Funciones repetidas:** elaboración de reportes, análisis de datos y automatización aparecen varias veces sin diferenciar alcance, complejidad o resultado.
- **Inventario técnico sin jerarquía:** mezcla tecnologías centrales con herramientas antiguas o no demostradas recientemente (QlikView, SPSS, R, Tableau, Hyperion, TM1, Toad, varios módulos SAP).
- **Redacción poco ejecutiva:** abundan expresiones como “realizar”, “responsable de” y explicaciones generales sobre lo que una herramienta permite hacer.
- **Cursos sin institución, fecha ni credencial:** reducen credibilidad ATS y ocupan espacio; “NPL” debe corregirse a “NLP” si se mantiene.
- **Falta de métricas:** no se explicita volumen de datos, periodicidad de reportes, cantidad de fuentes, cobertura funcional ni pruebas realizadas.
- **Brecha temporal visible:** agosto de 2017 a julio de 2018 no aparece explicado. No es obligatorio justificarlo en el CV, pero debe prepararse una respuesta para entrevista.
- **Perfil de salida centrado en lo que busca el candidato:** “estabilidad, crecimiento y aprendizaje” debe reemplazarse por el valor que aporta a la organización.

## Información que debe eliminarse

- Dirección completa.
- RUT.
- Fecha de nacimiento.
- Estado civil.
- Nacionalidad.
- Referencias a la relación contractual, previsional o familiar de la experiencia actual.
- Frases sin respaldo: “aumento significativo”, “consolidando la franquicia como líder” y equivalentes.
- Explicaciones extensas y genéricas sobre Excel, SQL o Python.
- Cursos sin emisor, fecha o respaldo, salvo validación posterior.
- Fotografía, gráficos de nivel, barras de progreso, íconos decorativos y tablas de maquetación.

## Información que debe agregarse

- Titular profesional orientado a Control de Gestión, BI y automatización.
- Resumen de 4 a 5 líneas que conecte negocio, gestión, datos, tecnología e IA aplicada.
- Diplomado en Advanced Business Analytics, FEN Universidad de Chile, en curso desde junio de 2026.
- Experiencia reciente actualizada como área funcional: **Control de Gestión, Business Intelligence y Automatización**.
- Logros verificables del repositorio: solución CRM, pipeline de captación multifuente, clasificación automática, RAG, reportes automatizados y alertas.
- Competencias agrupadas por uso: Gestión y Analytics; Datos y Automatización; IA aplicada; Visualización y herramientas de negocio.
- Ubicación general “Santiago, Chile”, teléfono y correo. Agregar LinkedIn solo cuando exista una URL revisada.
- Portafolio o enlace al proyecto solo si puede compartirse sin exponer datos, secretos ni información de clientes.

## Brechas frente a los cargos objetivo

| Cargo objetivo | Evidencia favorable | Brecha o riesgo | Acción recomendada |
|---|---|---|---|
| Analista Senior / Jefatura de Control de Gestión | Presupuesto, forecast, Capex/Opex, cierres, KPIs, análisis de variaciones, reportería y automatización | No hay métricas verificadas de presupuesto administrado, ahorro o personas a cargo | Cuantificar monto presupuestario, frecuencia de forecast, número de unidades y audiencias usuarias; no postular como jefatura con personas a cargo sin evidencia |
| BI / Business Analytics | Power BI y SQL declarados; Python, MongoDB, ETL, calidad y dashboards evidenciados | DAX, Power Query, modelamiento dimensional y data warehouse no están demostrados en el repositorio | Preparar casos concretos de Power BI/SQL y validar nivel actual de DAX, Power Query y modelamiento estrella |
| Business/Data Analyst | Experiencia de negocio, análisis, automatización e integración de fuentes | Falta portafolio público sanitizado y métricas de impacto económico | Crear un caso de estudio anonimizado con problema, datos, análisis, recomendación y resultado |
| Inteligencia Comercial / Planificación Comercial | CRM, scoring, segmentación, priorización, campañas y seguimiento de productividad | Forecast comercial avanzado y elasticidad/demanda no están completamente demostrados | Preparar ejemplo de planificación y explicar cómo se validan scores y recomendaciones |
| Automatización / Transformación Digital | APIs, formularios, scraping, reportes, MongoDB, firma digital y alertas | No hay medición comprobada de horas ahorradas o adopción | Levantar línea base manual, tiempo posterior y usuarios activos; marcar mientras tanto “requiere validación del candidato” |
| IA aplicada al negocio | LLM vía API, RAG, embeddings, clasificación, chatbot y búsqueda híbrida | Los “insights de dashboard” actuales son principalmente reglas determinísticas; no debe venderse todo como GenAI | Diferenciar reglas, scoring, búsqueda semántica y generación LLM; explicar supervisión, fallback y calidad |

## Palabras clave ATS prioritarias

### Control de Gestión y planificación

Control de gestión; presupuesto; budget; forecast; planificación financiera; planificación comercial; KPIs financieros y operacionales; análisis de desviaciones; cierre mensual; ingresos; costos; gastos; Capex; Opex; rentabilidad; productividad; reporting ejecutivo; cuadro de mando; mejora de procesos; toma de decisiones.

### Business Intelligence y Analytics

Business Intelligence; Business Analytics; Power BI; SQL; Python; Excel avanzado; ETL; integración de datos; modelamiento de datos; calidad de datos; dashboards; reportería automatizada; análisis descriptivo; análisis predictivo; segmentación; tendencias; insights accionables; MongoDB; APIs.

### Inteligencia comercial y automatización

CRM; gestión de leads; ciclo comercial; captación; scoring; priorización; SLA; alertas; automatización de procesos; transformación digital; web scraping; Playwright; FastAPI; RAG; embeddings; búsqueda semántica; LLM; IA generativa; chatbot; clasificación automática; recomendaciones.

Estas palabras coinciden con el lenguaje observado en vacantes chilenas recientes de Control de Gestión y Analytics, donde se repiten presupuesto/forecast, KPIs, Power BI, SQL, Python y automatización. Referencias: [Control de Gestión Senior](https://www.chiletrabajos.cl/trabajo/3820231), [Analista de Métricas y Proyectos](https://cl.redte.com/ad/181859/analista-metricas-y-proyectos-senior) e [Ingeniero de IA Aplicada](https://www.chiletrabajos.cl/trabajo/ingeniero-de-ia-aplicada-3843026).

## Recomendaciones concretas

1. Usar CV de una sola columna, sin tablas de maquetación, con títulos estándar y máximo dos páginas.
2. Mantener un CV maestro y tres variantes; no intentar cubrir Control de Gestión, BI e IA con el mismo orden de contenidos.
3. Limitar la experiencia reciente a 5-6 logros de mayor valor y las experiencias anteriores a 2-4 logros cada una.
4. Traducir tecnología a impacto: “integró dos portales y normalizó datos en MongoDB” es más útil que una lista de librerías.
5. Usar métricas solo cuando el artefacto sea trazable. Ejemplo defendible: “reporte automatizado sobre 2.086 registros de captación”.
6. Marcar como “requiere validación del candidato” cualquier reducción de tiempo, ahorro, aumento de ventas, usuarios activos, monto de presupuesto o personas lideradas.
7. Presentar la experiencia reciente como área funcional, no como cargo contractual: **Control de Gestión, Business Intelligence y Automatización**.
8. Preparar un portafolio anonimizado y una narrativa de entrevista que muestre el ciclo completo: necesidad comercial → diseño de datos → implementación → control de calidad → decisión.
9. Revisar y validar fechas exactas de la experiencia reciente antes de postular. El encargo indica continuidad actual, pero el CV fuente termina en noviembre de 2022.
10. No afirmar forecasting con IA, agentes autónomos ni dashboards generativos en producción mientras no exista evidencia adicional; el repositorio demuestra forecasting/insights parciales, búsqueda semántica y automatización inteligente, con distintos grados de madurez.

## Datos que requieren validación del candidato

- Fecha exacta de continuidad de la experiencia en Procasa desde diciembre de 2022.
- Monto y alcance de presupuestos/forecast administrados en GrandVision.
- Número de usuarios, ejecutivos y áreas que usan cada solución.
- Ahorro de horas, reducción de errores y frecuencia de actualización de reportes.
- Resultados comerciales atribuibles a dashboards, campañas o scoring.
- Nivel actual de Power BI, DAX, Power Query y SQL.
- Institución, fecha y credencial de cursos mencionados en el CV anterior.
- URL de LinkedIn y, si corresponde, portafolio público.
- Nivel de inglés.

