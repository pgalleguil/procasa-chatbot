# Banco de logros y preparación de entrevistas — Pablo Galleguillos

## 20 logros profesionales defendibles

1. Diseñó una solución integral de inteligencia comercial que conecta CRM, captación, propiedades, leads, actividades, alertas y reportería.
2. Automatizó un reporte de captaciones con seis hojas de análisis para 2.086 registros en el último corte inspeccionado.
3. Consolidó leads, cartera y captaciones en un libro de control de ocho hojas con métricas por ejecutivo, SLA y productividad.
4. Implementó un motor de inteligencia comercial sobre 462 propiedades, combinando precio, tasación, demanda y señales de leads.
5. Integró Yapo y TocToc mediante pipelines de adquisición, validación, normalización, clasificación y persistencia.
6. Conservó respaldos HTML y evidencia visual para auditoría, re-proceso y control de calidad de datos.
7. Implementó clasificación automática de anunciantes mediante reglas, scoring y un modelo de lenguaje vía API.
8. Diseñó estados de clasificación con evidencia, confianza, revisión manual y tratamiento de avisos eliminados.
9. Construyó un chatbot inmobiliario conectado con inventario, CRM y alertas comerciales.
10. Desarrolló un motor RAG que combina filtros estructurados, embeddings, similitud semántica y fallback geográfico.
11. Automatizó alertas de SLA, backlog y actividad con controles para evitar notificaciones duplicadas.
12. Digitalizó contratos y órdenes de visita mediante formularios web, validación, OTP, aceptación, PDF y seguimiento.
13. Desarrolló pipelines para extraer tasaciones desde PDF y convertirlas en variables de análisis comercial.
14. Implementó reportes y dashboards para seguimiento de KPIs, tendencias, rankings y planes de acción.
15. Participó en presupuesto anual de ventas, Capex/Opex y cierre financiero mensual en GrandVision.
16. Analizó variaciones de ingresos y costos contra presupuesto y preparó información para gestión.
17. Diseñó indicadores y cuadros de monitoreo de proyectos y ventas en Enel.
18. Gestionó conciliaciones, liquidaciones, provisiones, presupuestos y procesos transaccionales en Walmart Servicios Financieros.
19. Automatizó procesos y consultas de datos para apoyar decisiones en áreas operacionales y comerciales.
20. Incorporó backups, dry-runs, controles `$set`, auditorías y pruebas automatizadas en procesos de datos de mayor riesgo.

## Historias STAR

### 1. Control de captaciones y reportería

**Situación:** La gestión de captaciones estaba distribuida entre registros operativos y no existía una visión única de estados, productividad y SLA.

**Tarea:** Construir un sistema que permitiera a la administración monitorear cartera, gestión por ejecutivo, pendientes y cumplimiento.

**Acción:** Integré los datos en MongoDB, normalicé estados, definí KPIs y automaticé un libro de seis hojas con resumen ejecutivo, gestión, resultados, productividad, seguimiento y dashboard.

**Resultado:** El último reporte inspeccionado procesó 2.086 captaciones y entregó trazabilidad sobre gestionadas, pendientes, estados, actividad y SLA. El impacto en horas ahorradas requiere validación del candidato.

### 2. Priorización de cartera con inteligencia comercial

**Situación:** La cartera necesitaba una forma sistemática de identificar propiedades con sobreprecio, baja demanda o necesidad de acción.

**Tarea:** Traducir datos de precio, tasación y leads en prioridades comerciales.

**Acción:** Construí un pipeline de scoring y segmentación que compara precio publicado con tasación, incorpora demanda y actividad, recomienda acción y exporta detalle más resumen ejecutivo.

**Resultado:** El artefacto analizado cubre 462 propiedades y distingue casos de validación, ajuste de precio, campañas y oportunidades. El resultado comercial posterior requiere validación.

### 3. Clasificación automática de propietarios y corredores

**Situación:** La captación desde portales incluía avisos de propietarios, corredores, empresas y casos ambiguos; revisarlos manualmente no escalaba.

**Tarea:** Crear un proceso trazable que priorizara avisos y redujera falsos positivos.

**Acción:** Implementé reglas por identidad, marcas, lenguaje y señales de perfil; agregué scoring, evidencia, estados de revisión y escalamiento a LLM para casos no concluyentes.

**Resultado:** El flujo quedó integrado a los pipelines y a MongoDB, con trazabilidad por señal y manejo de avisos eliminados. No presento una tasa de exactitud porque no existe una evaluación productiva consolidada y vigente.

### 4. Integración de TocToc al CRM

**Situación:** Se requería ampliar la adquisición más allá de Yapo sin romper el esquema de datos del CRM.

**Tarea:** Incorporar una segunda fuente con campos, formatos y comportamiento dinámico diferentes.

**Acción:** Diseñé descubrimiento SSR, descarga HTTP con fallback Playwright, extracción, normalización a `crm_v1`, índice único y escritura `$set`.

**Resultado:** La corrida controlada insertó 12 documentos; 12/12 cumplieron ID, esquema, URL, validación HTML, imágenes y estado permitido. Se presenta como piloto, no como corrida masiva.

### 5. Chatbot y RAG para búsqueda de propiedades

**Situación:** Las consultas de clientes combinaban filtros objetivos con preferencias expresadas en lenguaje natural.

**Tarea:** Recomendar propiedades relevantes sin depender solo de coincidencias exactas.

**Acción:** Construí búsqueda híbrida con extracción de filtros, consulta MongoDB, embeddings, similitud coseno, exclusión de propiedades vistas y fallback por cercanía geográfica.

**Resultado:** El chatbot puede recuperar y contextualizar propiedades, registrar recomendaciones y derivar alertas. La precisión y conversión requieren validación con datos de uso.

### 6. Presupuesto y control financiero en GrandVision

**Situación:** El negocio necesitaba planificación anual y explicación mensual de resultados.

**Tarea:** Apoyar el presupuesto de ventas y Capex/Opex, y aportar análisis al cierre.

**Acción:** Consolidé información, analicé cuentas de resultados y balance, expliqué variaciones contra presupuesto y elaboré reportes/presentaciones de gestión.

**Resultado:** Se estableció un seguimiento periódico de KPIs e información para decisiones. Monto presupuestario y resultados cuantitativos requieren validación del candidato.

### 7. Control de proyectos comerciales en Enel

**Situación:** La unidad requería visibilidad sobre tareas, plazos, costos e indicadores de proyectos de ventas masivas.

**Tarea:** Cubrir el rol de Project Manager y mantener el control de ejecución.

**Acción:** Coordiné responsables, monitoreé avances, generé indicadores y automaticé reportes del área.

**Resultado:** La unidad contó con cuadros de monitoreo y reportería para seguimiento. Cantidad de proyectos y mejora de plazos requieren validación.

### 8. Control transaccional y mejora de procesos en Walmart

**Situación:** Los procesos adquirentes requerían cuadratura, liquidación, cobro y regularización de transacciones.

**Tarea:** Asegurar consistencia financiera y mejorar tareas operacionales repetitivas.

**Acción:** Realicé conciliaciones, liquidaciones, facturación, provisiones y consultas a bases de datos; además diseñé automatizaciones y propuestas de mejora.

**Resultado:** Se fortaleció el control operativo y la disponibilidad de información para decisiones. Volúmenes y ahorros requieren validación.

## Cómo explicar los proyectos tecnológicos

### Respuesta de 60 segundos

“No me presento como desarrollador puro. Mi fortaleza es entender un problema comercial o de control y llevarlo hasta una solución utilizable. En el proyecto inmobiliario partí por necesidades concretas: saber qué leads estaban sin gestión, qué captaciones priorizar y cómo integrar información de portales. Definí datos y KPIs, construí pipelines y un CRM, automaticé reportes y luego incorporé clasificación con IA, RAG y alertas. Lo importante no es la tecnología aislada, sino que la solución conecta operación, información y decisión.”

### Cómo explicar la IA sin exagerar

“Uso distintas capas según el problema. Las reglas y scores se aplican cuando necesito trazabilidad; los embeddings y RAG cuando la consulta es semántica; y los modelos de lenguaje cuando necesito interpretar texto o generar una respuesta contextual. También diseño fallbacks y revisión manual. No llamo IA a cualquier regla, y no afirmo exactitud sin una evaluación.”

### Cómo explicar el dashboard

“La torre de control consolida KPIs de leads, SLA, captaciones, productividad y tendencias en ocho vistas. Los insights actuales son principalmente automáticos y determinísticos; la integración generativa es una línea de evolución. Prefiero describir con precisión qué está en producción, qué es prototipo y qué está planificado.”

## Cómo explicar el trabajo en una empresa familiar

“La empresa tiene un vínculo familiar, pero mi aporte se evalúa por funciones y entregables concretos. He trabajado en control de gestión, inteligencia comercial y automatización: definí KPIs, construí reportería, integré datos, implementé CRM, scraping, clasificación y herramientas de apoyo comercial. Puedo demostrar el trabajo mediante sistemas, código, reportes y casos. No presento una relación contractual distinta de la real; me concentro en el valor profesional efectivamente aportado.”

## Por qué busca un cambio laboral

“El proyecto actual me permitió ampliar mucho mi perfil y construir soluciones end-to-end. Ahora busco un entorno con mayor escala, equipos multidisciplinarios y desafíos donde pueda aplicar esa combinación de control de gestión, analytics y automatización, con objetivos claros y posibilidades de crecer en responsabilidad. No busco alejarme del negocio inmobiliario por sí mismo; busco transferir capacidades que son aplicables a cualquier industria.”

## Expectativa salarial sin revelar remuneración actual

“Prefiero definir la expectativa según el alcance del cargo, la responsabilidad, la modalidad y el paquete total. Para una posición senior en control de gestión/BI en Santiago, me interesa una oferta competitiva con el mercado. Si me comparten la banda presupuestada, puedo confirmar rápidamente si estamos alineados. Mi remuneración actual no es una referencia comparable por la naturaleza del proyecto.”

Si exigen una cifra: “Considerando el alcance descrito, mi expectativa líquida/bruta está entre **[completar banda validada]**, conversable según beneficios y responsabilidad.” No improvisar una banda sin investigar la vacante y el mercado.

## Cómo defender la experiencia sin respaldo previsional completo

“La experiencia es real y verificable mediante entregables, repositorio, reportes, sistemas y referencias de usuarios del negocio. La forma administrativa de la relación no cambia las funciones realizadas. En el CV no atribuyo contrato, jefatura ni dependencia que no existan; describo el área de aporte, los proyectos implementados y los resultados que puedo demostrar. Si el proceso requiere antecedentes específicos, los conversaré de manera transparente.”

## Preguntas que debe preparar para entrevistas

1. ¿Cuál era el problema de negocio más importante que resolvió el CRM?
2. ¿Qué KPIs definió y por qué?
3. ¿Cómo validó la calidad y consistencia de los datos?
4. ¿Qué parte del proyecto desarrolló personalmente y qué apoyo recibió?
5. ¿Qué usuarios ocupan las soluciones y con qué frecuencia?
6. ¿Qué decisión cambió gracias a un dashboard o análisis?
7. ¿Cómo mide si un scoring o una recomendación funciona?
8. ¿Cómo evita alucinaciones o decisiones erróneas de un LLM?
9. ¿Cuándo prefiere reglas, SQL, ML o IA generativa?
10. ¿Cómo prioriza un backlog de automatizaciones?
11. ¿Qué haría distinto si reconstruyera la solución?
12. ¿Cómo abordaría un forecast con datos incompletos?
13. ¿Cómo explica una desviación presupuestaria a una jefatura no financiera?
14. ¿Qué nivel tiene en Power BI, DAX, Power Query y SQL? Preparar ejemplos concretos.
15. ¿Ha liderado personas? Responder solo con la experiencia real; distinguir liderazgo de proyecto y jefatura formal.
16. ¿Por qué terminó GrandVision en 2017 y qué ocurrió hasta 2018?
17. ¿Por qué el CV anterior terminaba Procasa en 2022?
18. ¿Qué métricas del proyecto puede compartir sin comprometer información confidencial?
19. ¿Cuál es su expectativa salarial y modalidad preferida?
20. ¿Qué espera lograr durante los primeros 90 días?

## Evidencia que conviene reunir antes de entrevistar

- Capturas anonimizadas de dashboards y CRM.
- Un reporte Excel sanitizado.
- Diagrama simple de arquitectura y flujo de datos.
- Una tabla antes/después con tiempos manuales, si puede validarse.
- Número de usuarios y frecuencia de uso.
- Monto o alcance de presupuesto/forecast de GrandVision.
- Ejemplos de SQL, Power BI y Python que pueda explicar de memoria.
- Referencia profesional que confirme funciones y proyectos.

