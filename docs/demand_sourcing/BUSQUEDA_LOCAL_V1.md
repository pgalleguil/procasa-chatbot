# Buscador inmobiliario local V1 (experimental)

No lee conversaciones, no escribe en Atlas ni contacta leads. Consume dos arrays JSON: catálogo normalizado (listing_key único, operation, property_type, commune, price_clp, price_valid, published, operational_state, source_type, timestamps de verificación, attributes, description/search_text opcional) y perfiles manuales. Ejemplo de perfil:
    
    {"profile_id":"demo","operation":"VENTA","comunas":["Maipú"],"property_types":["CASA"],"max_price_clp":100000000,"minimum_attributes":{"bedrooms":3},"query":"patio amplio"}

Desde la raíz del repositorio en Windows:

    python scripts/run_local_property_search.py --catalog catalogo.json --profiles perfiles.json --output resultados.json

Modo híbrido opcional (instalar sentence-transformers en un entorno virtual local separado, no en Render):

    python scripts/run_local_property_search.py --catalog catalogo.json --profiles perfiles.json --output resultados.json --model BAAI/bge-m3 --device cpu --allow-model-download

Usar --device cuda solo con GPU y PyTorch CUDA compatibles; quitar --allow-model-download después de descargar. El modelo es candidato, no ganador de un benchmark. En modo híbrido el catálogo se vectoriza una vez por ejecución; todavía no hay caché persistente ni reranker cross-encoder. No se reutilizan ni reemplazan los vectores del chatbot. El catálogo completo se evalúa antes de aplicar top-K. BM25 + similitud coseno fusionados por RRF. Restricciones duras preceden el ranking; los externos y evidencia incompleta permanecen en revisión. Las restricciones geográficas específicas quedan en revisión, no se calcula distancia en V1. El JSON no debe contener teléfonos ni conversaciones. La exportación real desde Atlas, descripción enriquecida, benchmark, deduplicación física entre portales, cache persistente y scraping real quedan pendientes. No activar el worker de producción.
