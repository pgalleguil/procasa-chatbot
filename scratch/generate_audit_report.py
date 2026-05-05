import os
from pymongo import MongoClient
from dotenv import load_dotenv

# Load config
load_dotenv(dotenv_path=r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\.env")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "URLS")
COLLECTION_UNIVERSO = "universo_cartera"
COLLECTION_TASACIONES = "tasaciones"
PDF_DIR = r"C:\Users\pgall\Desktop\Tasaciones"

def run_audit():
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    universo = db[COLLECTION_UNIVERSO]
    tasaciones = db[COLLECTION_TASACIONES]

    # 1. Get all codes that SHOULD be processed
    query = {
        "disponible": True,
        "oficina": "PROCASA SUCRE"
    }
    all_target_properties = list(universo.find(query, {"codigo": 1, "rol": 1, "comuna": 1}))
    target_codes = {p["codigo"] for p in all_target_properties if "codigo" in p}
    
    # 2. Get all codes already in the PDF folder
    pdf_files = [f for f in os.listdir(PDF_DIR) if f.endswith(".pdf")]
    downloaded_codes = {f.replace(".pdf", "") for f in pdf_files}
    
    # 3. Get all entries in the tasaciones collection to see status/reasons
    tasaciones_entries = list(tasaciones.find({}, {"codigo_propiedad": 1, "status": 1, "mensaje": 1, "error_msg": 1}))
    tasaciones_map = {str(t["codigo_propiedad"]): t for t in tasaciones_entries}

    missing_codes = []
    failed_codes = []
    
    for prop in all_target_properties:
        codigo = str(prop["codigo"])
        if codigo in downloaded_codes:
            continue
        
        # If not downloaded, check if it was attempted
        t_info = tasaciones_map.get(codigo)
        if t_info:
            reason = t_info.get("mensaje") or t_info.get("error_msg") or t_info.get("status")
            failed_codes.append({
                "codigo": codigo,
                "rol": prop.get("rol"),
                "comuna": prop.get("comuna"),
                "reason": reason
            })
        else:
            missing_codes.append({
                "codigo": codigo,
                "rol": prop.get("rol"),
                "comuna": prop.get("comuna"),
                "reason": "Pendiente / No intentado"
            })

    print(f"Total Target Properties: {len(all_target_properties)}")
    print(f"Total Downloaded PDFs: {len(downloaded_codes)}")
    print(f"Total Failed/Missing: {len(failed_codes) + len(missing_codes)}")
    print("\n--- DETALLE DE FALLAS ---")
    for f in failed_codes:
        print(f"Codigo: {f['codigo']} | Rol: {f['rol']} | Comuna: {f['comuna']} | Motivo: {f['reason']}")
    
    print("\n--- PENDIENTES (Aun no procesados por el script) ---")
    for m in missing_codes:
        print(f"Codigo: {m['codigo']} | Rol: {m['rol']} | Comuna: {m['comuna']}")

if __name__ == "__main__":
    run_audit()
