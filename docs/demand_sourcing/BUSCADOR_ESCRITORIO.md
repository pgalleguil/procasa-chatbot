# Buscador inmobiliario local de escritorio

La app Tkinter consulta el snapshot JSON local con BGE-M3 y `bge-reranker-v2-m3`. El índice, las consultas y los resultados permanecen en este equipo. No lee conversaciones, no contacta leads y no escribe en MongoDB ni en producción.

## Abrir en Windows

Usa el acceso directo **PROCASA – Buscador inmobiliario** del Escritorio. Su destino es `scripts/abrir_buscador_inmobiliario.cmd`. También se puede ejecutar ese `.cmd` directamente desde el worktree.

El lanzador ya apunta a los recursos locales preparados:

- Snapshot preparado y sanitizado: `C:\Users\pgall\Desktop\Python\procasa-search-local-results-20261126\catalog_desktop.json` (26.508 fichas). El original queda intacto.
- Caché incremental: `C:\Users\pgall\Desktop\Python\procasa-search-local-results-20261126\benchmark-20261126-variants\bge_m3_reranker\cache\bge_m3_reranker-cuda.sqlite`.
- Modelos locales en `benchmark-20261126-run03\hf-cache`, sin descargas.
- BGE-M3 `5617a9f61b028005a4858fdac845db406aefb181`; reranker `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41`.
- CUDA, embeddings FP16 con lote 16 y reranker FP16 con lote 4.

El catálogo y la caché están fuera del repositorio. No copiar ninguno al control de versiones.

## Buscar

1. Espera a que la barra indique que cargó el catálogo y los modelos.
2. Selecciona `VENTA` o `ARRIENDO`; ingresa comuna, tipo de propiedad y presupuesto máximo CLP.
3. Completa dormitorios, baños, estacionamientos, bodegas y superficies si son requisitos obligatorios. El texto libre sirve para ordenar preferencias semánticas; no convierte una mención en un requisito confirmado.
4. Pulsa **Buscar**. **Búsqueda masiva JSON** acepta hasta 1.000 perfiles manuales estructurados. **Abrir anuncio** abre el enlace web del resultado seleccionado. **Exportar resultados** guarda un JSON local nuevo.
5. Las filas separan resultados con verificación registrada de los que siguen por revisar. La búsqueda no confirma vigencia actual.

Los precios se filtran en CLP. Si falta `price_clp`, solo se calcula una referencia desde `price_uf` cuando la moneda explícita es UF y el snapshot trae `price_conversion_uf_clp`; la fila se marca con `≈` y la ficha conserva la fecha y tasa. No se estima una conversión para monedas desconocidas.

El snapshot disponible no contiene región ni sector en sus campos; la ficha muestra **Sector no informado** en lugar de inferirlo desde la comuna o la descripción. Dormitorios, baños, superficies y características se muestran con etiquetas en español solo cuando existen en los datos.

## Refrescar el snapshot de Atlas en solo lectura

La app **Actualizar catálogo local** vuelve a leer el archivo indicado al iniciarla; no consulta Atlas. Para preparar una versión nueva:

1. Conecta Atlas con una cuenta de base de datos que tenga únicamente permiso `read`. Lee solamente `universo_cartera_prop360` y `propiedades_captacion`, que son las colecciones del snapshot actual. No consultes conversaciones, CRM, leads, propietarios ni contactos.
2. Usa una proyección inclusiva de campos inmobiliarios públicos y normaliza el resultado al esquema del snapshot: `listing_key`, `source_type`, `operation`, `property_type`, `commune`, `title`, `description`, `canonical_url`, precios y su moneda/tasa/fecha, estado de publicación, atributos y características. No exportes documentos completos. El campo de sector solo se rellena si existe explícitamente en la fuente.
3. Guarda la salida normalizada como un JSON candidato fuera del repositorio. Pásala por la lista permitida y los filtros de texto del preflight, generando otro archivo candidato:

   ```powershell
   .\.venvs\procasa-search-validation-20261126\Scripts\python.exe scripts\prepare_local_property_search_catalog.py --input "RUTA\exportacion-normalizada.json" --output "RUTA\catalogo-candidato.json"
   ```

   El preparador conserva únicamente campos inmobiliarios públicos, elimina claves privadas dentro de características, y redacta teléfonos y correos. No modifica el JSON de entrada.

4. Valídala antes de usarla:

   ```powershell
   .\.venvs\procasa-search-validation-20261126\Scripts\python.exe scripts\validate_local_property_search_catalog.py --catalog "RUTA\catalogo-candidato.json"
   ```

   El preflight es local y de solo lectura. Exige claves únicas, campos públicos permitidos y ausencia de campos/contactos privados; informa filas, SHA-256, fuente y operación.
5. Conserva una copia local del snapshot anterior. Sustituye `catalog_desktop.json` por el candidato validado fuera del repositorio y pulsa **Actualizar catálogo local**. Se recalculan los vectores solo para textos nuevos o modificados; los textos iguales se reutilizan desde SQLite.

No uses credenciales con permiso de escritura para este refresco. La app y el preflight no contienen llamadas a MongoDB. No guardes snapshots, credenciales, caché ni resultados de búsqueda en Git.
