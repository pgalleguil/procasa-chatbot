
from pymongo import MongoClient
from config import Config

def check_marcela():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    coll = db["universo_cartera"]

    # Buscar propiedades de Marcela Machuca en Procasa Sucre que estén disponibles
    query = {
        "oficina": "PROCASA SUCRE",
        "ejecutivo": {"$regex": "Marcela Machuca", "$options": "i"},
        "disponible": True
    }

    count = coll.count_documents(query)
    print(f"Propiedades activas de Marcela Machuca en PROCASA SUCRE: {count}")

    if count > 0:
        print("\nListado de propiedades encontradas:")
        for doc in coll.find(query):
            print(f"- Código: {doc.get('codigo')}, Ejecutivo: {doc.get('ejecutivo')}")
    else:
        print("\nConfirmado: No hay propiedades activas a nombre de Marcela Machuca en PROCASA SUCRE.")

    # Adicionalmente, verificar si existen propiedades de ella en otras oficinas si es relevante
    query_all = {
        "ejecutivo": {"$regex": "Marcela Machuca", "$options": "i"},
        "disponible": True
    }
    count_all = coll.count_documents(query_all)
    if count_all > count:
        print(f"\nNota: Se encontraron {count_all - count} propiedades activas de Marcela Machuca en OTRAS oficinas.")

if __name__ == "__main__":
    check_marcela()
