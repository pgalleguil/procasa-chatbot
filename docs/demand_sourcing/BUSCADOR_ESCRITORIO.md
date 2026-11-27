# Buscador local de escritorio — BGE-M3

Usar el mismo entorno de Python, modelos y catálogo normalizado que Codex utilizó en el benchmark (26.508 avisos). No se requiere un servicio web ni cambiar producción.

Desde PowerShell en el worktree de validación, con el entorno virtual activado:

    python scripts/local_property_search_app.py --catalog "RUTA_REAL_CATALOGO.json" --cache "RUTA_CACHE_LOCAL.sqlite" --revision SHA_BGE_M3 --reranker-revision SHA_RERANKER --device cuda

Los SHA exactos se leen de comparison_metrics.json; no usar valores inventados. Se permite indicar carpetas locales completas mediante --model y --reranker. Si el benchmark usó una implementación diferente de embeddings (p.ej. FP16), Codex debe comprobar la identidad del caché antes de reutilizarlo; usar una caché nueva en caso de duda. No descargar modelos salvo --allow-model-download explícito.

- Ingresar operación, comunas, tipos y presupuesto CLP máximo; especificar mínimos obligatorios en sus campos. La descripción libre solo determina preferencias semánticas y no garantiza atributos obligatorios. Referencias geográficas libres siempre quedan en revisión.
- Buscar o cargar un array JSON de hasta 1000 perfiles manuales ya estructurados. El runner CLI existente sigue siendo la opción para campañas largas con checkpoints.
- El índice se conserva en memoria entre consultas; Actualizar catálogo local relee el snapshot y reutiliza vectores de textos sin cambios. No obtiene un nuevo snapshot de Atlas por sí solo.
- Los resultados muestran ajuste a filtros, estado de verificación, fuente, detalles, enlace y razones. Verificación registrada significa evidencia existente en el snapshot, no confirmación de disponibilidad en tiempo real. Anuncios externos nunca se convierten en autorizados por similitud.
- Exportar JSON local con las alternativas y sus estados. No envía mensajes ni escribe MongoDB.
- Ejecutar python -m pytest -c pytest-procasa.ini tests/test_local_property_search.py tests/test_local_property_search_app.py -q y python scripts/local_property_search_app.py --help. Verificar apertura real de Tkinter, primera carga, consulta, recarga con una ficha modificada y exportación en Windows.
- Pendientes: integrar exportador de catálogo de Codex, refrescar fuentes en solo lectura, verificar portales y disponibilidad, validar resultados reales, deduplicar propiedades físicas. No activar scraping ni conversación.
