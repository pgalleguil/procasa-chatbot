import os
import sys
import asyncio
import unicodedata
import re
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from motor.motor_asyncio import AsyncIOMotorClient
from scraping.scraping_yapo_proxys import is_likely_broker, _BROKER_KEYWORDS

def normalize(text):
    if not text: return ""
    text = str(text).lower().strip()
    text = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', text)

async def main():
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]

    # Buscar TODOS los corredores históricos
    query = {"details.es_propietario_directo": False}

    print("Calculando el total de registros históricos a procesar...")
    total_corredores = await coll.count_documents(query)
    
    if total_corredores == 0:
        print("No hay corredores para procesar.")
        return

    print(f"Iniciando saneamiento masivo de {total_corredores} registros...")

    total_revisados = 0
    reclasificados = 0
    corregidos_por_company = 0
    corregidos_por_profile = 0

    from tqdm import tqdm

    with tqdm(total=total_corredores, desc="Saneando", unit="reg") as pbar:
        while True:
            # Paginación por lotes de 1000 para evitar cursor timeouts
            records = await coll.find(query).sort("_id", 1).limit(1000).to_list(length=1000)
            
            if not records:
                break
                
            for doc in records:
                total_revisados += 1

                details = doc.get("details", {})
                pub = details.get("publicador", "N/A")
                comp = details.get("company_name", "N/A")
                
                # 1. Mitigación del bug de company_name
                test_comp = comp
                pub_n = normalize(pub)
                comp_n = normalize(comp)
                
                fue_bug_company = False
                if pub != "N/A" and comp != "N/A" and pub_n == comp_n:
                    if not any(kw in pub.lower() for kw in _BROKER_KEYWORDS):
                        test_comp = "N/A"
                        fue_bug_company = True
                        
                # 2. Reevaluar con is_likely_broker (que ya no puntúa por profile_id ni seller_is_pro)
                desc = details.get("descripcion_corta", details.get("descripcion", "N/A"))
                prof_id = details.get("seller_profile_id", "N/A")
                is_pro = details.get("seller_is_pro", False)
                
                es_corredor = is_likely_broker(pub, desc, test_comp, prof_id, is_pro)
                
                if not es_corredor:
                    # Es un propietario directo (falso positivo)
                    update_data = {
                        "details.es_propietario_directo": True,
                        "details.confianza_propietario": 0.95,
                        "details.audit_fix": "historical_false_positive_cleanup"
                    }
                    if test_comp == "N/A" and comp != "N/A":
                        update_data["details.company_name"] = "N/A"
                        
                    await coll.update_one({"_id": doc["_id"]}, {"$set": update_data})
                    
                    reclasificados += 1
                    if fue_bug_company:
                        corregidos_por_company += 1
                    else:
                        corregidos_por_profile += 1
                
                pbar.update(1)
                        
            # Actualizar query para la siguiente iteración
            last_id = records[-1]["_id"]
            query = {"details.es_propietario_directo": False, "_id": {"$gt": last_id}}
    
    print("\n")
    out = []
    out.append("==================================================================")
    out.append(f"REPORTE FINAL: SANEAMIENTO HISTÓRICO ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC)")
    out.append("==================================================================")
    out.append(f"Total de falsos corredores revisados: {total_revisados}")
    out.append(f"Total de dueños reales recuperados:   {reclasificados}")
    out.append("Desglose de reparaciones:")
    out.append(f"  -> Afectados por bug de company_name: {corregidos_por_company}")
    out.append(f"  -> Afectados por bug de profile_id:   {corregidos_por_profile}")
    out.append("==================================================================")
    
    report = "\n".join(out)
    print(report)
    
    with open("fix_historical_all_report.txt", "w", encoding="utf-8") as f:
        f.write(report)

    client.close()

if __name__ == "__main__":
    asyncio.run(main())
