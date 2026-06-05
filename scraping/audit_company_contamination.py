import os
import sys
import asyncio
import unicodedata
import re
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from motor.motor_asyncio import AsyncIOMotorClient

def normalize(text):
    if not text: return ""
    text = str(text).lower().strip()
    text = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', text)

async def main():
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]

    # Registros de AYER y HOY
    yesterday_start = (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    query = {"details.fecha_scraping": {"$gte": yesterday_start}}
    
    records = await coll.find(query).to_list(length=None)
    
    if not records:
        query = {"fecha_captura": {"$gte": (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)}}
        records = await coll.find(query).to_list(length=None)

    total_registros = len(records)
    if total_registros == 0:
        msg = "No se encontraron registros de ayer ni hoy."
        print(msg)
        with open("audit_company_contamination.txt", "w", encoding="utf-8") as f:
            f.write(msg)
        return

    print("Iniciando auditoría final de contaminación de company_name...")

    total_corredores = 0
    casos_criterio = []
    
    enterprise_words = [
        "propiedades", "inmobiliaria", "corretaje", "corredora", 
        "remax", "re/max", "engel", "houm", "procasa", "fuenzalida", "exp"
    ]

    for idx, doc in enumerate(records, 1):
        if idx % 50 == 0 or idx == total_registros:
            print(f"Procesando {idx}/{total_registros} ({(idx/total_registros)*100:.1f}%)", end="\r", flush=True)
            
        details = doc.get("details", {})
        es_dueno = details.get("es_propietario_directo", False)
        
        if not es_dueno:
            total_corredores += 1
            
            pub = details.get("publicador", "N/A")
            comp = details.get("company_name", "N/A")
            brok = details.get("broker_brand", "N/A")
            prof_id = details.get("seller_profile_id", "N/A")
            is_pro = details.get("seller_is_pro", False)
            
            if comp == "N/A" or pub == "N/A":
                continue
                
            pub_n = normalize(pub)
            comp_n = normalize(comp)
            
            # Criterio A: company_name == publicador
            if pub_n == comp_n:
                # Criterio B: No contiene palabras empresariales
                is_empresarial = any(ew in comp_n for ew in enterprise_words)
                if not is_empresarial:
                    casos_criterio.append({
                        "url": doc.get("url", "N/A"),
                        "publicador": pub,
                        "company_name": comp,
                        "broker_brand": brok,
                        "seller_profile_id": prof_id,
                        "seller_is_pro": is_pro
                    })

    print() # Salto de línea
    
    # Análisis de la SECCIÓN 5
    solo_company = 0
    con_profile = 0
    con_pro = 0
    con_broker = 0
    
    total_casos = len(casos_criterio)
    
    for c in casos_criterio:
        has_prof = c["seller_profile_id"] != "N/A" and c["seller_profile_id"] is not None and c["seller_profile_id"] != ""
        has_pro = c["seller_is_pro"] == True
        has_brok = c["broker_brand"] != "N/A" and c["broker_brand"] is not None and c["broker_brand"] != ""
        
        if has_prof: con_profile += 1
        if has_pro: con_pro += 1
        if has_brok: con_broker += 1
        
        if not has_prof and not has_pro and not has_brok:
            solo_company += 1

    out = []
    out.append("==================================================================")
    out.append("SECCION 1")
    out.append("==================================================================")
    out.append(f"Total registros ayer+hoy: {total_registros}")
    
    out.append("\n==================================================================")
    out.append("SECCION 2")
    out.append("==================================================================")
    out.append(f"Total corredores: {total_corredores}")
    
    out.append("\n==================================================================")
    out.append("SECCION 3")
    out.append("==================================================================")
    out.append(f"Cantidad donde company_name == publicador (sin palabras empresariales): {total_casos}")
    
    out.append("\n==================================================================")
    out.append("SECCION 4")
    out.append("==================================================================")
    out.append("Primeros 100 ejemplos:\n")
    for ej in casos_criterio[:100]:
        out.append(f"URL: {ej['url']}")
        out.append(f"PUBLICADOR: {ej['publicador']}")
        out.append(f"COMPANY_NAME: {ej['company_name']}")
        out.append(f"BROKER_BRAND: {ej['broker_brand']}")
        out.append(f"SELLER_PROFILE_ID: {ej['seller_profile_id']}")
        out.append(f"SELLER_IS_PRO: {ej['seller_is_pro']}")
        out.append("---")
        
    out.append("\n==================================================================")
    out.append("SECCION 5")
    out.append("==================================================================")
    out.append("RESUMEN DE ESTOS CASOS:\n")
    out.append(f"- Dependen SOLAMENTE de company_name: {solo_company}")
    out.append(f"- Además tienen seller_profile_id: {con_profile}")
    out.append(f"- Además tienen seller_is_pro: {con_pro}")
    out.append(f"- Además tienen broker_brand: {con_broker}")
    
    out.append("\n==================================================================")
    out.append("SECCION 6")
    out.append("==================================================================")
    out.append("CONCLUSIÓN AUTOMÁTICA\n")
    
    if total_casos > 0:
        casos_con_otras = len([c for c in casos_criterio if (c["seller_profile_id"] != "N/A" and c["seller_profile_id"] != "") or c["seller_is_pro"] == True or (c["broker_brand"] != "N/A" and c["broker_brand"] != "")])
        real_pct_otras = (casos_con_otras / total_casos) * 100
        pct_solo_company = (solo_company / total_casos) * 100
        
        if real_pct_otras > 70:
            out.append("NO HAY EVIDENCIA DE QUE company_name SEA LA CAUSA PRINCIPAL")
        elif pct_solo_company > 70:
            out.append("ALTA PROBABILIDAD DE CONTAMINACIÓN")
        else:
            out.append("RESULTADO MIXTO. REVISAR DATOS MANUALMENTE.")
    else:
        out.append("NO SE ENCONTRARON CASOS.")

    report = "\n".join(out)
    print(report)
    with open("audit_company_contamination.txt", "w", encoding="utf-8") as f:
        f.write(report)
        
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
