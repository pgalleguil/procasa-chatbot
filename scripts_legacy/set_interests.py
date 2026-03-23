
from pymongo import MongoClient
from config import Config

def set_user_interests():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    usuarios = db["usuarios"]
    
    interests = {
        "PABLO": ["Vitacura", "Lo Barnechea"],
        "PEDRO": ["Las Condes"],
        "MATIAS": ["Santiago", "Providencia"]
    }
    
    for name, communes in interests.items():
        # Buscamos por nombre (insensible a mayúsculas para mayor seguridad)
        result = usuarios.update_many(
            {"nombre": {"$regex": f"^{name}$", "$options": "i"}},
            {"$set": {"comunas_interes": communes}}
        )
        print(f"Usuario {name}: {result.modified_count} documentos actualizados con {communes}")

    # Verificar resultados
    print("\nEstado actual de intereses:")
    for user in usuarios.find({"comunas_interes": {"$exists": True}}):
        print(f"- {user.get('nombre')}: {user.get('comunas_interes')}")

if __name__ == "__main__":
    set_user_interests()
