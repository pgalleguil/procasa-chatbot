import os
from pymongo import MongoClient
from dotenv import load_dotenv

# Configuración
BASE_DIR = r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok"
load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "URLS")
COLLECTION_TASACIONES = "tasaciones"

def cleanup_failures():
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    tasaciones = db[COLLECTION_TASACIONES]

    # Contar antes de borrar
    total_antes = tasaciones.count_documents({})
    exitos = tasaciones.count_documents({"status": "exito_informe_completo"})
    a_borrar = total_antes - exitos

    print(f"Total registros en 'tasaciones': {total_antes}")
    print(f"Registros exitosos (se mantienen): {exitos}")
    print(f"Registros de falla/error detectados: {a_borrar}")

    if a_borrar > 0:
        # Borrar todo lo que NO sea éxito
        result = tasaciones.delete_many({"status": {"$ne": "exito_informe_completo"}})
        print(f"¡Limpieza completada! Se eliminaron {result.deleted_count} registros de fallas.")
    else:
        print("No se encontraron registros de falla para eliminar.")

if __name__ == "__main__":
    cleanup_failures()
