# Buscador inmobiliario local V1 (experimental)

No lee conversaciones, no escribe en Atlas ni contacta leads. Consume dos arrays JSON: catálogo normalizado (listing_key único, operation, property_type, commune, price_clp, price_valid, published, operational_state, source_type, timestamps de verificación, attributes, description/search_text opcional) y perfiles manuales. Ejemplo de perfil:
    
    {"profile_id":"demo","operation":"VENTA","comunas":["Maipú"],"property_types":["CASA"],"max_price_clp":100000000,"minimum_attributes":{"bedrooms":3},"query":"patio amplio"}

Desde la raíz del repositorio en Windows:

    python scripts/run_local_property_search.py --catalog catalogo.json --profiles perfiles.json --output resultados.json

Modo híbrido opcional (instalar sentence-transformers en un entorno virtual local separado, no en Render):

    python scripts/run_local_property_search.py --catalog catalogo.json --profiles perfiles.json --output resultados.json --model BAAI/bge-m3 --device cpu --allow-model-download

Usar --device cuda solo con GPU y PyTorch CUDA compatibles; quitar --allow-model-download después de descargar. El modelo es candidato, no ganador de un benchmark. En modo híbrido el catálogo se vectoriza una vez por ejecución y usa caché SQLite persistente si se entrega `--cache`. No se reutilizan ni reemplazan los vectores del chatbot. El catálogo completo se evalúa antes de aplicar top-K. BM25 + similitud coseno se fusionan por RRF. Restricciones duras preceden el ranking; los externos y la evidencia incompleta permanecen en revisión. Las restricciones geográficas específicas quedan en revisión si no hay coordenadas verificadas en ambos extremos y un radio positivo. El JSON no debe contener teléfonos, correos, conversaciones ni datos de contacto.


## V2 — caché, reranking y reanudación

- SQLite local: --cache embeddings.sqlite; reutiliza textos sin cambios y guarda lotes completos. Requiere --revision con SHA de 40 caracteres o ruta local de modelo. No sobrescribe vectores del chatbot.
- --reranker BAAI/bge-reranker-v2-m3 y --rerank-pool 50 son opciones experimentales. Mantiene las listas compatible y revisión separadas. No convierte un puntaje en verificación.
- --resume retoma un JSON de salida existente, con los mismos argumentos, inputs y versiones inmutables de los modelos. Para obtener resultados frescos, iniciar otro archivo: las verificaciones del checkpoint usan su fecha inicial.
- Distancias Haversine solo con coordenadas marcadas como verificadas en ambos extremos y radio positivo.
- El procesamiento se detiene con Ctrl+C; perfiles ya escritos se conservan. No ejecutar dos procesos sobre el mismo archivo de salida.
- El benchmark local compara MiniLM + filtros, BM25, BGE-M3 y BGE-M3 + BGE-reranker-v2-m3 sobre la misma exportación y perfiles sintéticos. Congela cada modelo con una revisión SHA inmutable y separa la caché por modelo y revisión.
- La salida del benchmark incluye latencia por perfil, tiempos de carga e indexación fría/caliente, actualización de un aviso, RAM/VRAM, tasa de consultas, violaciones de requisitos y hojas de evaluación ciega. P@5, R@10 y nDCG@10 solo se calculan al contar con etiquetas de relevancia humanas independientes.
- Para una exportación de catálogo, fijar `--as-of` al `exported_at_utc` del resumen y guardar todo el resultado fuera del repositorio. No versionar el catálogo, salidas, hojas de revisión ni cachés locales.
- La rama de validación no escribe en Atlas ni accede a colecciones de CRM o conversaciones. No activar el worker de producción.


## Rama aislada de validación
Separa el buscador local del PR #245 (atrasado respecto de main). No incorpora worker de demanda, webhook, CRM ni despliegue. La consulta libre expresa preferencias; requisitos y exclusiones obligatorios van en campos estructurados. Ejecutar comparaciones con la misma exportación fechada y perfiles, etiquetas humanas independientes, y medir métricas de relevancia solo después de etiquetar a ciegas. No elegir un modelo por tamaño sin benchmark.
