
from pymongo import MongoClient
from config import Config

def inspect_collections():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    # Inspeccionar universo_cartera
    doc_cartera = db["universo_cartera"].find_one()
    print("Muestra universo_cartera:")
    print(f"Campos: {list(doc_cartera.keys()) if doc_cartera else 'Vacío'}")
    if doc_cartera:
        print(f"Ejemplo: Código={doc_cartera.get('codigo')}, Ejecutivo={doc_cartera.get('ejecutivo')}")

    # Inspeccionar universo_obelix
    doc_obelix = db["universo_obelix"].find_one()
    print("\nMuestra universo_obelix:")
    print(f"Campos: {list(doc_obelix.keys()) if doc_obelix else 'Vacío'}")
    if doc_obelix:
        # Algunos posibles nombres de campos en obelix basados en tareas previas
        # (A veces usan 'cod_propiedad', 'ejecutivo', 'responsable', etc.)
        print(f"Ejemplo: Código={doc_obelix.get('codigo')}, Ejecutivo={doc_obelix.get('ejecutivo') or doc_obelix.get('responsable')}")

if __name__ == "__main__":
    inspect_collections()
