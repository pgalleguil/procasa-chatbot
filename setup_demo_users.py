
from pymongo import MongoClient
from config import Config

def setup_demo_users():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    usuarios = db["usuarios"]
    
    # Comunas de ejemplo segun pedido (o lo que se asume del contexto)
    # PABLO: Vitacura, Lo Barnechea
    # PEDRO: Las Condes
    # MATIAS: Santiago, Providencia
    
    demo_data = [
        {"nombre": "Pablo Ejemplo", "username": "pablo_demo", "rol": "agente", "comunas_interes": ["Vitacura", "Lo Barnechea"]},
        {"nombre": "Pedro Ejemplo", "username": "pedro_demo", "rol": "agente", "comunas_interes": ["Las Condes"]},
        {"nombre": "Matias Ejemplo", "username": "matias_demo", "rol": "agente", "comunas_interes": ["Santiago", "Providencia"]}
    ]
    
    for user in demo_data:
        # Intentar actualizar por username o nombre si ya existen similares
        result = usuarios.update_one(
            {"username": user["username"]},
            {"$set": user},
            upsert=True
        )
        if result.upserted_id:
            print(f"Creado usuario: {user['nombre']} ({user['username']}) - Comunas: {user['comunas_interes']}")
        else:
            print(f"Actualizado usuario: {user['nombre']} ({user['username']}) - Comunas: {user['comunas_interes']}")

    print("\nValidación final de usuarios con intereses:")
    for u in usuarios.find({"comunas_interes": {"$exists": True, "$not": {"$size": 0}}}):
        print(f"- {u['nombre']}: {u['comunas_interes']}")

if __name__ == "__main__":
    setup_demo_users()
