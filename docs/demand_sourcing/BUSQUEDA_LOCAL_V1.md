# Motor de búsqueda inmobiliaria local

No lee conversaciones, no escribe en Atlas ni contacta leads. Consume dos arrays JSON: catálogo normalizado (listing_key único, operation, property_type, commune, price_clp, price_valid, published, operational_state, source_type, timestamps de verificación, attributes, description/search_text opcional) y perfiles manuales. Ejemplo de perfil:
    
    {"profile_id":"demo","operation":"VENTA","comunas":["Maipú"],"property_types":["CASA"],"max_price_clp":100000000,"minimum_attributes":{"bedrooms":3},"query":"patio amplio"}

Desde la raíz del repositorio en Windows:

    python scripts/run_local_property_search.py --catalog catalogo.json --profiles perfiles.json --output resultados.json

Modo híbrido para ejecución local (no en Render):

    python scripts/run_local_property_search.py --catalog catalogo.json --profiles perfiles.json --output resultados.json --model BAAI/bge-m3 --device cpu --allow-model-download

Usar --device cuda solo con GPU y PyTorch CUDA compatibles. Para el escritorio, BGE-M3 y BGE-reranker-v2-m3 son la configuración seleccionada, fijada a SHA y ejecutada con modelos locales. En modo híbrido el catálogo se vectoriza una vez por ejecución y usa caché SQLite persistente si se entrega `--cache`. No se reutilizan ni reemplazan los vectores del chatbot. El catálogo completo se evalúa antes de aplicar top-K. BM25 + similitud coseno se fusionan por RRF. Restricciones duras preceden el ranking; los externos y la evidencia incompleta permanecen en revisión. Las restricciones geográficas específicas quedan en revisión si no hay coordenadas verificadas en ambos extremos y un radio positivo. El JSON no debe contener teléfonos, correos, conversaciones ni datos de contacto.


## V2 — caché, reranking y reanudación

- SQLite local: --cache embeddings.sqlite; reutiliza textos sin cambios y guarda lotes completos. Requiere --revision con SHA de 40 caracteres o ruta local de modelo. No sobrescribe vectores del chatbot.
- --reranker BAAI/bge-reranker-v2-m3 y --rerank-pool 50 mantienen las listas compatible y revisión separadas. Un puntaje nunca equivale a verificación.
- --resume retoma un JSON de salida existente, con los mismos argumentos, inputs y versiones inmutables de los modelos. Para obtener resultados frescos, iniciar otro archivo: las verificaciones del checkpoint usan su fecha inicial.
- Distancias Haversine solo con coordenadas marcadas como verificadas en ambos extremos y radio positivo.
- El procesamiento se detiene con Ctrl+C; perfiles ya escritos se conservan. No ejecutar dos procesos sobre el mismo archivo de salida.
- Los benchmarks guardados son evidencia histórica de validación; no forman parte del uso de escritorio ni se ejecutan desde el lanzador.
- Para una exportación de catálogo, fijar `--as-of` al `exported_at_utc` del resumen y guardar todo el resultado fuera del repositorio. No versionar el catálogo, salidas, hojas de revisión ni cachés locales.
- La rama de validación no escribe en Atlas ni accede a colecciones de CRM o conversaciones. No activar el worker de producción.


## Rama aislada de validación
Separa el buscador local del chatbot y de producción. No incorpora worker de demanda, webhook, CRM ni despliegue. La consulta libre expresa preferencias; requisitos y exclusiones obligatorios van en campos estructurados.
