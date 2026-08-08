"""
=============================================================================
  GENERADOR DE EMBEDDINGS - Herramienta Standalone
=============================================================================
  USO: Ejecutar desde la RAÍZ del proyecto:
  
    cd C:\\Users\\pgall\\Desktop\\Python\\ChatBot_v4_Grok
    python run_embeddings.py
  
  PRIMERA VEZ: Descarga el modelo (~80MB) y genera embeddings para TODAS
  las propiedades con descripción. Las siguientes ejecuciones solo procesan
  las propiedades nuevas que no tengan vector.
=============================================================================
"""
import sys
import os
import logging

# Configurar logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("embeddings")

# Asegurar que el directorio actual es el del proyecto
os.chdir(os.path.dirname(os.path.abspath(__file__)))

def main():
    print("=" * 60)
    print("  GENERADOR DE EMBEDDINGS - ProCasa CRM")
    print("=" * 60)
    
    # 1. Verificar dependencias
    try:
        from fastembed import TextEmbedding
        print("[OK] fastembed instalado")
    except ImportError:
        print("[ERROR] Falta instalar fastembed.")
        print("  Ejecuta: pip install fastembed")
        sys.exit(1)
    
    try:
        from sklearn.metrics.pairwise import cosine_similarity
        print("[OK] scikit-learn instalado")
    except ImportError:
        print("[ERROR] Falta instalar scikit-learn.")
        print("  Ejecuta: pip install scikit-learn")
        sys.exit(1)
    
    try:
        import numpy
        print("[OK] numpy instalado")
    except ImportError:
        print("[ERROR] Falta instalar numpy.")
        print("  Ejecuta: pip install numpy")
        sys.exit(1)
    
    # 2. Conectar a MongoDB
    print("\n--- Conectando a MongoDB ---")
    try:
        from chatbot.storage import get_db
        from config import Config
        db = get_db()
        collection = db[Config.COLLECTION_NAME]
        total_props = collection.count_documents({})
        print(f"[OK] Conectado. Propiedades totales: {total_props}")
    except Exception as e:
        print(f"[ERROR] No se pudo conectar a MongoDB: {e}")
        sys.exit(1)
    
    # 3. Limpieza de descripciones
    print("\n--- Paso 1: Limpieza de Descripciones ---")
    from chatbot.semantic_engine import clean_desc_for_embedding, update_embeddings_bulk, build_embedding_text

    pending_clean = collection.count_documents({
        "descripcion": {"$exists": True, "$ne": ""},
        "descripcion_clean": {"$exists": False}
    })
    already_clean = collection.count_documents({"descripcion_clean": {"$exists": True}})
    print(f"  Ya limpias: {already_clean}")
    print(f"  Pendientes (esquema plano): {pending_clean}")

    # 4. Generación de Embeddings
    print("\n--- Paso 2: Generacion de Embeddings ---")
    pending_vectors = collection.count_documents({
        "vector_descripcion": {"$exists": False},
        "$or": [
            {"descripcion_clean": {"$exists": True, "$ne": ""}},
            {"observaciones.descripcion": {"$exists": True, "$nin": ["", None]}},
        ],
    })
    already_vectors = collection.count_documents({"vector_descripcion": {"$exists": True}})
    print(f"  Con vector: {already_vectors}")
    print(f"  Sin vector (pendientes): {pending_vectors}")
    
    if pending_clean == 0 and pending_vectors == 0:
        print("\n[OK] Todo actualizado. No hay nada que procesar.")
        return
    
    # 5. Procesar en lotes
    total_processed = 0
    batch_size = 100
    print(f"\n--- Procesando en lotes de {batch_size} ---")
    print("  (La primera vez se descargara el modelo, ~80MB, esto tarda ~1 min)")
    
    while True:
        count = update_embeddings_bulk(batch_size=batch_size)
        if count == 0:
            break
        total_processed += count
        print(f"  Procesados: {total_processed} embeddings...")
    
    # 6. Resumen final
    final_vectors = collection.count_documents({"vector_descripcion": {"$exists": True}})
    print(f"\n{'=' * 60}")
    print(f"  RESUMEN FINAL")
    print(f"  Embeddings generados en esta ejecucion: {total_processed}")
    print(f"  Total propiedades con vector: {final_vectors}/{total_props}")
    print(f"{'=' * 60}")
    print(f"\n[LISTO] Ya puedes usar la busqueda semantica desde el CRM.")


if __name__ == "__main__":
    main()
