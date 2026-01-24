import logging
from pymongo import MongoClient
from config import Config

def obtener_aceptados_2026():
    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
        collection = db[Config.COLLECTION_CONTACTOS]

        # 1. Definimos las campañas de este mes (Enero 2026)
        campanas_2026 = [
            "ajuste_precio_202601",
            "ajuste_precio_regiones_202601",
            "ajuste_precio_202601_REPASO",
            "ajuste_precio_regiones_202601_REPASO",
            "ajuste_precio_202601_TERCER",
            "ajuste_precio_regiones_202601_TERCER",
            "ajuste_precio_202601_REPASO_TERCER"
        ]

        # 2. La consulta corregida según el diagnóstico
        query = {
            "estado": "ajuste_autorizado",  # El estado real según el log
            "update_price.campana_nombre": {"$in": campanas_2026}
        }

        proyeccion = {
            "codigo": 1, 
            "nombre_propietario": 1, 
            "email_propietario": 1,
            "update_price.campana_nombre": 1,
            "_id": 0
        }

        aceptados = list(collection.find(query, proyeccion))

        if not aceptados:
            print("\n🔍 No se encontraron aceptaciones con el estado 'ajuste_autorizado' para las campañas 2026.")
            return

        # 3. Mostrar resultados
        print(f"\n✅ PROPIEDADES PARA DESTACAR (ENERO 2026) - Total: {len(aceptados)}")
        print("-" * 90)
        print(f"{'CÓDIGO':<10} | {'PROPIETARIO':<25} | {'CAMPAÑA'}")
        print("-" * 90)

        for doc in aceptados:
            codigo = doc.get("codigo", "S/C")
            nombre = doc.get("nombre_propietario", "Sin Nombre").title()
            campana = doc.get("update_price", {}).get("campana_nombre", "N/A")
            
            print(f"{codigo:<10} | {nombre[:25]:<25} | {campana}")

    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        client.close()

if __name__ == "__main__":
    obtener_aceptados_2026()