
import sys
import os
from pymongo import MongoClient
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from config import Config

def init_db():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    # 1. Update Usuarios
    print("Updating 'usuarios' collection...")
    res_users = db["usuarios"].update_many(
        {"comunas_interes": {"$exists": False}},
        {"$set": {"comunas_interes": []}}
    )
    print(f"Updated {res_users.modified_count} users with empty 'comunas_interes'.")
    
    # 2. Update Yapo Propiedades
    print("Updating 'yapo_propiedades' collection...")
    # Initial management object for existing properties
    # es_propietario_directo must be true for sourcing
    res_yapo = db["yapo_propiedades"].update_many(
        {
            "es_propietario_directo": True,
            "gestion": {"$exists": False}
        },
        {"$set": {
            "gestion": {
                "estado": "NUEVO",
                "ejecutivo_asignado": None,
                "fecha_asignacion": None,
                "intentos_contacto": 0,
                "notas": []
            }
        }}
    )
    print(f"Updated {res_yapo.modified_count} properties with default 'gestion' object.")

if __name__ == "__main__":
    init_db()
