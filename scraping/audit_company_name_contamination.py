import os
import sys
import asyncio
import re
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from motor.motor_asyncio import AsyncIOMotorClient

def normalize(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', str(text).strip().lower())

async def main():
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    query = {"details.fecha_scraping": {"$gte": today_start}}
    records = await coll.find(query).to_list(length=None)
    
    if not records:
        query = {"fecha_captura": {"$gte": datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)}}
        records = await coll.find(query).to_list(length=None)

    total_registros = len(records)
    if total_registros == 0:
        msg = "No se encontraron registros de la última ejecución (hoy)."
        print(msg)
        with open("audit_company_name_contamination.txt", "w", encoding="utf-8") as f:
            f.write(msg)
        return

    company_eq_publicador = 0
    company_eq_broker = 0
    company_empresarial = 0
    company_parece_persona = 0
    
    enterprise_words = [
        "propiedades", "inmobiliaria", "realty", "remax", "re/max", 
        "corredores", "broker", "corretaje", "houm", "engel", "fuenzalida", "exp"
    ]
    
    ejemplos = []

    print("Iniciando auditoría de contaminación en company_name...")

    for idx, doc in enumerate(records, 1):
        if idx % 50 == 0 or idx == total_registros:
            print(f"Procesando {idx}/{total_registros} ({(idx/total_registros)*100:.1f}%)", end="\r", flush=True)
            
        details = doc.get("details", {})
        pub = details.get("publicador", "N/A")
        comp = details.get("company_name", "N/A")
        brok = details.get("broker_brand", "N/A")
        
        if comp == "N/A":
            continue
            
        pub_n = normalize(pub)
        comp_n = normalize(comp)
        brok_n = normalize(brok)
        
        # 1. company_name == publicador
        es_igual_pub = False
        if pub_n and comp_n and pub_n == comp_n:
            company_eq_publicador += 1
            es_igual_pub = True
            if len(ejemplos) < 100:
                ejemplos.append({
                    "url": doc.get("url", "N/A"),
                    "publicador": pub,
                    "company_name": comp
                })
                
        # 2. company_name == broker_brand
        if comp_n and brok_n and brok_n != "n/a" and comp_n == brok_n:
            company_eq_broker += 1
            
        # 3. palabras empresariales
        is_empresarial = any(ew in comp_n for ew in enterprise_words)
        if is_empresarial:
            company_empresarial += 1
        elif es_igual_pub:
            # Si no es empresarial y es igual al publicador, es altamente probable que sea una persona
            company_parece_persona += 1

    print() # Salto de línea al terminar progreso

    out = []
    out.append("==================================================================")
    out.append("RESULTADOS AUDITORÍA: CONTAMINACIÓN COMPANY_NAME")
    out.append("==================================================================")
    out.append(f"TOTAL REGISTROS ANALIZADOS: {total_registros}")
    out.append(f"\n1. Registros con company_name == publicador: {company_eq_publicador}")
    out.append(f"2. Registros con company_name == broker_brand: {company_eq_broker}")
    out.append(f"3. Registros con company_name que contienen palabras empresariales: {company_empresarial}")
    out.append(f"4. Registros con company_name que parece persona (igual a publicador y sin keywords): {company_parece_persona}")
    
    out.append("\n==================================================================")
    out.append("5. EJEMPLOS DE COMPANY_NAME == PUBLICADOR (Hasta 100)")
    out.append("==================================================================")
    for ej in ejemplos:
        out.append(f"URL: {ej['url']}")
        out.append(f"PUBLICADOR: {ej['publicador']}")
        out.append(f"COMPANY_NAME: {ej['company_name']}")
        out.append("---")
        
    report = "\n".join(out)
    print(report)
    with open("audit_company_name_contamination.txt", "w", encoding="utf-8") as f:
        f.write(report)
        
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
