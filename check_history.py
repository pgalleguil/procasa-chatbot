
from pymongo import MongoClient
from config import Config
from datetime import datetime

def check_history():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    coll = db["universo_cartera"]

    # Buscar cambios de ejecutivo en el historial para la oficina PROCASA SUCRE
    # que hayan ocurrido recientemente (hoy)
    today = datetime.now().strftime("%Y-%m-%d")
    
    pipeline = [
        {"$match": {"oficina": "PROCASA SUCRE"}},
        {"$unwind": "$historial_cambios"},
        {"$match": {
            "historial_cambios.campo": "ejecutivo",
            "historial_cambios.valor_anterior": {"$regex": "Marcela Machuca", "$options": "i"},
            "historial_cambios.fecha": {"$regex": f"^{today}"}
        }},
        {"$project": {
            "codigo": 1,
            "anterior": "$historial_cambios.valor_anterior",
            "nuevo": "$historial_cambios.valor_nuevo",
            "fecha": "$historial_cambios.fecha"
        }}
    ]

    results = list(coll.aggregate(pipeline))
    print(f"Cambios de ejecutivo detectados hoy (Marcela Machuca -> Otros) en PROCASA SUCRE: {len(results)}")

    if results:
        print("\nEjemplos de reasignación:")
        for res in results[:10]: # Mostrar los primeros 10
            print(f"- Propiedad {res['codigo']}: {res['anterior']} -> {res['nuevo']}")
    else:
        # Si no hay cambios hoy, quizás ya se habían cambiado antes. 
        # Busquemos quiénes son los ejecutivos actuales en PROCASA SUCRE para dar visibilidad.
        print("\nNo se detectaron cambios de Marcela Machuca en la última ejecución (probablemente ya estaban actualizados).")
        
        print("\nDistribución de ejecutivos actuales en PROCASA SUCRE (Activos):")
        dist = coll.aggregate([
            {"$match": {"oficina": "PROCASA SUCRE", "disponible": True}},
            {"$group": {"_id": "$ejecutivo", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ])
        for d in dist:
            print(f"- {d['_id']}: {d['count']} propiedades")

if __name__ == "__main__":
    check_history()
