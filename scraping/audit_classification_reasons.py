import os
import sys
import asyncio
import random
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from motor.motor_asyncio import AsyncIOMotorClient

async def main():
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]

    # Filtro Obligatorio: Propiedades creadas hoy
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
        with open("audit_classification_reasons.txt", "w", encoding="utf-8") as f:
            f.write(msg)
        return

    print("Iniciando análisis de razones de clasificación...")

    duenos = 0
    corredores = 0
    
    # Sec 2 variables
    corr_con_profile = 0
    corr_con_pro = 0
    corr_con_broker = 0
    corr_con_company = 0
    
    # Sec 3 variables
    solo_profile = 0
    solo_pro = 0
    solo_broker = 0
    solo_company = 0
    
    # Sec 5 variables
    company_names = Counter()
    broker_brands = Counter()
    publicadores = Counter()
    
    # Sec 4 list
    lista_corredores = []

    for idx, doc in enumerate(records, 1):
        if idx % 50 == 0 or idx == total_registros:
            print(f"Procesando {idx}/{total_registros} ({(idx/total_registros)*100:.1f}%)", end="\r", flush=True)
            
        details = doc.get("details", {})
        es_dueno = details.get("es_propietario_directo", False)
        
        if es_dueno:
            duenos += 1
        else:
            corredores += 1
            
            prof_id = details.get("seller_profile_id", "N/A")
            is_pro = details.get("seller_is_pro", False)
            b_brand = details.get("broker_brand", "N/A")
            c_name = details.get("company_name", "N/A")
            
            has_prof = prof_id != "N/A" and prof_id is not None and prof_id != ""
            has_pro = is_pro == True
            has_broker = b_brand != "N/A" and b_brand is not None and b_brand != ""
            has_company = c_name != "N/A" and c_name is not None and c_name != ""
            
            signals = 0
            motivos = []
            if has_prof:
                corr_con_profile += 1
                signals += 1
                motivos.append("profile_id")
            if has_pro:
                corr_con_pro += 1
                signals += 1
                motivos.append("seller_is_pro")
            if has_broker:
                corr_con_broker += 1
                signals += 1
                motivos.append("broker_brand")
            if has_company:
                corr_con_company += 1
                signals += 1
                motivos.append("company_name")
                
            if signals == 1:
                if has_prof: solo_profile += 1
                if has_pro: solo_pro += 1
                if has_broker: solo_broker += 1
                if has_company: solo_company += 1
                
            publicador = details.get("publicador", "N/A")
                
            if c_name != "N/A": company_names[c_name] += 1
            if b_brand != "N/A": broker_brands[b_brand] += 1
            if publicador != "N/A": publicadores[publicador] += 1
            
            lista_corredores.append({
                "url": doc.get("url", "N/A"),
                "publicador": publicador,
                "profile_id": prof_id,
                "seller_is_pro": is_pro,
                "broker_brand": b_brand,
                "company_name": c_name,
                "es_propietario_directo": es_dueno,
                "motivos": motivos
            })
            
    print() # Salto de línea

    out = []
    out.append("==================================================================")
    out.append("SECCIÓN 1")
    out.append("=========")
    out.append(f"TOTAL ANALIZADOS: {total_registros}")
    out.append(f"DUEÑOS: {duenos}")
    out.append(f"CORREDORES: {corredores}")
    
    out.append("\n==================================================================")
    out.append("SECCIÓN 2")
    out.append("=========")
    out.append(f"CORREDORES CON PROFILE_ID: {corr_con_profile}")
    out.append(f"CORREDORES CON SELLER_IS_PRO: {corr_con_pro}")
    out.append(f"CORREDORES CON BROKER_BRAND: {corr_con_broker}")
    out.append(f"CORREDORES CON COMPANY_NAME: {corr_con_company}")
    
    out.append("\n==================================================================")
    out.append("SECCIÓN 3")
    out.append("=========")
    out.append("CORREDORES CLASIFICADOS ÚNICAMENTE POR UNA SEÑAL:")
    out.append(f"SOLO PROFILE_ID: {solo_profile}")
    out.append(f"SOLO SELLER_IS_PRO: {solo_pro}")
    out.append(f"SOLO BROKER_BRAND: {solo_broker}")
    out.append(f"SOLO COMPANY_NAME: {solo_company}")
    
    out.append("\n==================================================================")
    out.append("SECCIÓN 4")
    out.append("=========")
    out.append("50 CORREDORES ALEATORIOS:\n")
    
    if len(lista_corredores) > 50:
        sample_corredores = random.sample(lista_corredores, 50)
    else:
        sample_corredores = lista_corredores
        
    for s in sample_corredores:
        out.append(f"URL: {s['url']}")
        out.append(f"PUBLICADOR: {s['publicador']}")
        out.append(f"SELLER_PROFILE_ID: {s['profile_id']}")
        out.append(f"SELLER_IS_PRO: {s['seller_is_pro']}")
        out.append(f"BROKER_BRAND: {s['broker_brand']}")
        out.append(f"COMPANY_NAME: {s['company_name']}")
        out.append(f"ES_PROPIETARIO_DIRECTO: {s['es_propietario_directo']}")
        out.append(f"MOTIVOS DETECTADOS:")
        for m in s['motivos']:
            out.append(f"* {m}")
        out.append("---")
        
    out.append("\n==================================================================")
    out.append("SECCIÓN 5")
    out.append("=========")
    out.append("TOP 50 COMPANY_NAME\n")
    for name, count in company_names.most_common(50):
        out.append(f"{name} -> {count}")
        
    out.append("\nTOP 50 BROKER_BRAND\n")
    for name, count in broker_brands.most_common(50):
        out.append(f"{name} -> {count}")
        
    out.append("\nTOP 50 PUBLICADOR\n")
    for name, count in publicadores.most_common(50):
        out.append(f"{name} -> {count}")
        
    out.append("\n==================================================================")
    out.append("SECCIÓN 6")
    out.append("=========")
    out.append("CONCLUSIONES AUTOMÁTICAS\n")
    
    if corredores > 0:
        senales = {
            "profile_id": corr_con_profile,
            "seller_is_pro": corr_con_pro,
            "broker_brand": corr_con_broker,
            "company_name": corr_con_company
        }
        sorted_senales = sorted(senales.items(), key=lambda x: x[1], reverse=True)
        
        dominant = sorted_senales[0]
        secondary = sorted_senales[1] if len(sorted_senales) > 1 else ("N/A", 0)
        
        out.append(f"SEÑAL DOMINANTE:")
        out.append(f"{dominant[0]}")
        out.append(f"\nSEÑAL SECUNDARIA:")
        out.append(f"{secondary[0]}")
        out.append(f"\nRIESGO:")
        out.append(f"{dominant[0]} está presente en el {(dominant[1]/corredores)*100:.0f}% de los corredores.\n")
    else:
        out.append("No hay corredores para analizar.\n")
        
    report_text = "\n".join(out)
    print(report_text)
    
    with open("audit_classification_reasons.txt", "w", encoding="utf-8") as f:
        f.write(report_text)

    client.close()

if __name__ == "__main__":
    asyncio.run(main())
