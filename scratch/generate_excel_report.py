import os
import pandas as pd
from pymongo import MongoClient
from dotenv import load_dotenv

# Configuración de rutas
BASE_DIR = r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok"
load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "URLS")
COLLECTION_UNIVERSO = "universo_cartera"
COLLECTION_TASACIONES = "tasaciones"
PDF_DIR = r"C:\Users\pgall\Desktop\Tasaciones"
OUTPUT_FILE = os.path.join(os.path.expanduser("~"), "Desktop", "Reporte_Fallas_Tasaciones.xlsx")

def generate_excel_v3():
    print("Conectando a MongoDB...")
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    universo = db[COLLECTION_UNIVERSO]
    tasaciones = db[COLLECTION_TASACIONES]

    print("Consultando base de datos...")
    query = {"disponible": True, "oficina": "PROCASA SUCRE"}
    all_props = list(universo.find(query))
    
    # Obtener códigos con PDF descargado
    if not os.path.exists(PDF_DIR):
        downloaded_codes = set()
    else:
        downloaded_codes = {f.replace(".pdf", "") for f in os.listdir(PDF_DIR) if f.endswith(".pdf")}
    
    # Obtener histórico de intentos
    tasaciones_entries = list(tasaciones.find())
    tasaciones_map = {str(t["codigo_propiedad"]): t for t in tasaciones_entries}

    report_data = []

    for prop in all_props:
        codigo = str(prop.get("codigo", ""))
        status_final = "PENDIENTE"
        motivo = ""
        
        if codigo in downloaded_codes:
            status_final = "EXITO (PDF Descargado)"
        else:
            t_info = tasaciones_map.get(codigo)
            if t_info:
                status_final = "FALLA"
                motivo = t_info.get("mensaje") or t_info.get("error_msg") or t_info.get("status")
            else:
                status_final = "NO INTENTADO"
                motivo = "Sin procesar aún"

        # Armar fila con los campos CORRECTOS de la base de datos
        # dormitorios -> Habitaciones
        # banos -> Baños
        # direccion_propietario -> Dirección
        report_data.append({
            "Código": codigo,
            "Rol": prop.get("rol"),
            "Comuna": prop.get("comuna"),
            "Dirección": prop.get("direccion_propietario", ""),
            "Estado": status_final,
            "Motivo Falla": motivo,
            "Habitaciones": prop.get("dormitorios"),
            "Baños": prop.get("banos"),
            "Precio UF": prop.get("precio_uf")
        })

    df = pd.DataFrame(report_data)
    
    # Reordenar columnas
    cols = ["Código", "Rol", "Comuna", "Dirección", "Estado", "Motivo Falla", "Habitaciones", "Baños", "Precio UF"]
    df = df[cols]
    
    # Ordenar por estado
    def sort_logic(x):
        if "FALLA" in x: return 0
        if "PENDIENTE" in x or "NO INTENTADO" in x: return 1
        return 2
    
    df["sort_key"] = df["Estado"].apply(sort_logic)
    df = df.sort_values(by=["sort_key", "Código"]).drop(columns=["sort_key"])

    print(f"Exportando a Excel: {OUTPUT_FILE}")
    df.to_excel(OUTPUT_FILE, index=False)
    print("¡Reporte listo!")

if __name__ == "__main__":
    try:
        generate_excel_v3()
    except Exception as e:
        print(f"Error: {e}")
