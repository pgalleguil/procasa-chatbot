
import pandas as pd
from pymongo import MongoClient
from config import Config
from tqdm import tqdm

def audit_executives():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    coll_cartera = db["universo_cartera"]
    coll_obelix = db["universo_obelix"]

    print("Cargando datos de universo_obelix...")
    # Creamos un mapa de código (todas las variantes) -> ejecutivo
    obelix_data = {}
    for doc in coll_obelix.find({}, {"codigo": 1, "codigo_convecta": 1, "codigo_procasa": 1, "ejecutivo": 1}):
        ejec = str(doc.get("ejecutivo") or "").strip()
        if not ejec:
            continue
            
        cods = []
        if doc.get("codigo"): cods.append(str(doc.get("codigo")).strip())
        if doc.get("codigo_convecta"): cods.append(str(doc.get("codigo_convecta")).strip())
        if doc.get("codigo_procasa"): cods.append(str(doc.get("codigo_procasa")).strip())
        
        for cod in set(cods):
            if cod:
                obelix_data[cod] = ejec

    print(f"Códigos mapeados desde Obelix: {len(obelix_data)}")

    print("\nAuditando oficina PROCASA SUCRE en universo_cartera...")
    query_cartera = {"oficina": "PROCASA SUCRE"}
    
    diferencias = []
    total_revisadas = 0

    for doc in tqdm(coll_cartera.find(query_cartera), desc="Auditoría"):
        total_revisadas += 1
        cod = str(doc.get("codigo")).strip()
        ejec_cartera = str(doc.get("ejecutivo") or "").strip()
        
        # Cruzar con Obelix
        if cod in obelix_data:
            ejec_obelix = obelix_data[cod]
            if ejec_cartera.lower() != ejec_obelix.lower():
                diferencias.append({
                    "codigo_cartera": cod,
                    "oficina": doc.get("oficina"),
                    "ejecutivo_cartera": ejec_cartera,
                    "ejecutivo_obelix_correcto": ejec_obelix
                })

    print(f"\nRevision finalizada.")
    print(f"Total propiedades revisadas en Sucre: {total_revisadas}")
    print(f"Diferencias encontradas: {len(diferencias)}")

    if diferencias:
        df = pd.DataFrame(diferencias)
        output_file = "diferencias_ejecutivos_sucre.xlsx"
        df.to_excel(output_file, index=False)
        print(f"\nArchivo generado: {output_file}")
    else:
        print("\nNo se encontraron diferencias de ejecutivos en PROCASA SUCRE.")

if __name__ == "__main__":
    audit_executives()
