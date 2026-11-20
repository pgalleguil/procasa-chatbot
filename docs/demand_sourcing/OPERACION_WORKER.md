# Operación del motor permanente de oportunidades

## Estado en este PR

El código agrega el catálogo materializado, casos por ciclo de asignación, matching en ambos sentidos, bandeja administrativa, alertas internas, jobs agrupados y un ejecutable de worker separado. El worker no está desplegado ni habilitado por este cambio. No se crearon servicios cloud, no se fusionó el PR y no se enviaron mensajes a compradores o anunciantes.

El flujo de casos y catálogo solo opera cuando se configura `PROCASA_DEMAND_SOURCING_ENABLED=1` en el proceso web y en el worker. El scraping externo además requiere `PROCASA_DEMAND_SOURCING_EXTERNAL_SCRAPE_ENABLED=true` en el worker. Ambos valores están apagados por defecto. Con scraping apagado, los jobs procesan el catálogo disponible y muestran el motivo de degradación.

## Configurar el worker separado

Crear un Background Worker con el mismo commit y dependencias del servicio PROCASA. Comando de inicio:

```bash
python scripts/run_demand_sourcing_worker.py --poll-seconds 5 --batch-size 200
```

Configurar el mismo `MONGO_URI` y `DB_NAME` del CRM, además de:

```text
PROCASA_DEMAND_SOURCING_ENABLED=1
PROCASA_DEMAND_SOURCING_EXTERNAL_SCRAPE_ENABLED=false
PROCASA_DEMAND_SOURCING_DAILY_QUOTA=20
```

El worker crea los índices de las colecciones nuevas, backfillea el catálogo por lotes con checkpoint, procesa cambios incrementales y revisa hasta 20 búsquedas activas por ciclo. Si MongoDB no admite change streams, usa una lectura acotada de actualizaciones recientes cada cinco minutos. Los jobs usan leases, reintentos y cuota diaria UTC independiente por portal. El `--batch-size` se limita internamente a 1–1000; cada búsqueda externa tiene límites de páginas, URLs y resultados.

Habilitar `PROCASA_DEMAND_SOURCING_EXTERNAL_SCRAPE_ENABLED=true` solo después de revisar las condiciones del portal, cuota y costo operacional. El adaptador Yapo usa el `YAPO_SEARCH_URL_TEMPLATE` ya configurado y el CLI existente, con búsqueda segmentada por comuna y tipo; no dispone de filtros estrictos de tipo o precio, por lo que el motor vuelve a aplicar el tope CLP individual después del scraping. TocToc usa sus opciones actuales de operación, tipo, comuna, publicador particular y límites; el precio de la página de resultados no se considera evidencia, y el tope se valida con el detalle.

Ambos adaptadores usan `dry-run`, deshabilitan llamadas LLM y distribución posterior, pasan un ID de lote aleatorio y limpian los reportes/HTML de ese lote al terminar. La escritura al catálogo de demanda guarda referencias y datos comerciales mínimos, no teléfono, email, nombre de anunciante ni contenido de conversación. Si un proceso se termina forzosamente durante una descarga, pueden quedar archivos de ese lote en el directorio del scraper; se identifican por el prefijo `demand_` y deben revisarse antes de eliminarlos.

## Revisión y entrega

La bandeja `/crm/captacion-demanda` requiere sesión de `admin`, `supervisor` o `jefatura`. Los anuncios externos permanecen como prospectos: la entrega interna exige confirmación humana de identidad, disponibilidad, precio y autorización comercial, con vigencia de 24 horas y precio dentro del presupuesto duro del comprador. Los registros internos PROCASA también requieren disponibilidad y precio vigentes cuando la ficha no tiene evidencia reciente.

La acción de entrega crea únicamente una tarea de CRM asignada al ejecutivo actual del comprador. No envía WhatsApp, no cambia la titularidad del anuncio y no altera comisiones o SLA. Cambios de ciclo, revisión de criterios o exclusiones invalidan relaciones anteriores; cada entrega vuelve a validar el ciclo vigente y los requisitos.

## Salud, degradación y rollback

La pantalla muestra el número de fichas indexadas, avance del backfill, heartbeat, último job, errores por fuente y cobertura de segmentos. `python scripts/run_demand_sourcing_worker.py --once` procesa un ciclo y sale; debe usarse solo con un Mongo de desarrollo o con una ventana aprobada de operación. Los tests de este PR usan datos sintéticos y adaptadores simulados; no ejecutan scrapers reales.

Para pausar, detener el Background Worker y configurar ambos flags como apagados en web y worker. Las colecciones y checkpoints se conservan; no hay cambios destructivos en las colecciones fuente. Para retirar el código, usar el proceso normal de reversión del PR; no borrar las colecciones de demanda como parte de un rollback operativo.

## Pendiente antes de operar en producción

- Configurar y habilitar un Background Worker separado; este PR no despliega infraestructura.
- Confirmar permisos Mongo para crear índices, change streams y checkpoints en el cluster destino.
- Aprobar externamente el uso/costo de Yapo y TocToc antes de encender el flag de scraping.
- Ejecutar un canary con fuentes habilitadas y revisar cobertura, duración, límites y archivos residuales antes de ampliar cuotas.
- La ubicación por referencia escolar requiere coordenadas verificadas del punto y de cada ficha. Las fichas sin geometría verificada quedan en revisión geográfica y no se entregan como compatibles.
- No se declaran cifras de latencia, precisión ni costo: todavía no hay benchmarks de producción.
