import os
import sys
import re
import asyncio
import hashlib
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
        # Fallback si fecha_scraping no captura nada, intentamos las últimas 24 hrs por _id o fecha_captura
        query = {"fecha_captura": {"$gte": datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)}}
        records = await coll.find(query).to_list(length=None)

    total_registros = len(records)
    
    if total_registros == 0:
        msg = "No se encontraron registros de la última ejecución (hoy)."
        print(msg)
        with open("audit_today_report.txt", "w", encoding="utf-8") as f:
            f.write(msg)
        return

    # Contadores
    duenos = 0
    corredores = 0
    
    profile_present = 0
    profile_absent = 0
    
    sospechosos_list = []
    
    html_with_profile = 0
    html_without_profile = 0
    
    html_contact_logo = 0
    html_avatar = 0
    html_badge_pro = 0
    html_profile_id = 0

    htmls_analizados = 0

    html_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "html_dumps")
    html_dir_exists = os.path.exists(html_dir)
    total_htmls_en_carpeta = len(os.listdir(html_dir)) if html_dir_exists else 0
    
    print(f"HTML DIR: {html_dir}")
    print(f"EXISTE: {html_dir_exists}")
    print(f"TOTAL HTMLS EN CARPETA: {total_htmls_en_carpeta}")
    print("Iniciando análisis de registros...")

    company_names = Counter()
    broker_brands = Counter()

    total_count = len(records)
    for idx, doc in enumerate(records, 1):
        if idx % 50 == 0 or idx == total_count:
            print(f"Procesando {idx}/{total_count} ({(idx/total_count)*100:.1f}%)", end="\r", flush=True)
            
        details = doc.get("details", {})
        
        # Sec 1: Dueños vs Corredores
        es_dueno = details.get("es_propietario_directo", False)
        if es_dueno:
            duenos += 1
        else:
            corredores += 1
            
        # Sec 2: profile_id
        # Algunos guardan en seller_profile_id, si es dict o campo principal
        prof_id = details.get("seller_profile_id", "N/A")
        if prof_id != "N/A" and prof_id is not None and prof_id != "":
            profile_present += 1
            has_profile = True
        else:
            profile_absent += 1
            has_profile = False
            
        # Sec 3 y 4: Casos Sospechosos
        broker_brand = details.get("broker_brand", "N/A")
        company_name = details.get("company_name", "N/A")
        seller_is_pro = details.get("seller_is_pro", False)
        
        if not es_dueno and has_profile and broker_brand == "N/A" and company_name == "N/A" and not seller_is_pro:
            sospechosos_list.append({
                "url": doc.get("url", "N/A"),
                "publicador": details.get("publicador", "N/A"),
                "profile_id": prof_id,
                "broker_brand": broker_brand,
                "company_name": company_name,
                "seller_is_pro": seller_is_pro,
                "es_propietario_directo": es_dueno
            })
            
        if company_name != "N/A":
            company_names[company_name] += 1
        else:
            company_names["N/A"] += 1
            
        if broker_brand != "N/A":
            broker_brands[broker_brand] += 1
        else:
            broker_brands["N/A"] += 1
            
        # Sec 5 y 6: HTML Dumps
        url = doc.get("url", "")
        if url:
            filename = hashlib.md5(url.encode()).hexdigest() + ".html"
            html_path = os.path.join(html_dir, filename)
            
            if os.path.exists(html_path):
                try:
                    with open(html_path, "r", encoding="utf-8") as f:
                        html_content = f.read()
                        
                    htmls_analizados += 1
                    
                    if "/user/profile/id/" in html_content:
                        html_with_profile += 1
                        html_profile_id += 1
                    else:
                        html_without_profile += 1
                        
                    if "contact_logo" in html_content:
                        html_contact_logo += 1
                    if "advertiser-avatar" in html_content:
                        html_avatar += 1
                    if 'title="Profesional"' in html_content or 'title=\'Profesional\'' in html_content:
                        html_badge_pro += 1
                        
                except Exception:
                    pass

    print() # Salto de línea después de la barra de progreso
    # --- Generar Reporte ---
    out = []
    
    out.append("==================================================================")
    out.append("SECCIÓN 1")
    out.append("=========")
    out.append(f"TOTAL REGISTROS ANALIZADOS: {total_registros}")
    out.append(f"DUEÑOS: {duenos}")
    out.append(f"CORREDORES: {corredores}")
    out.append(f"PORCENTAJES: Dueños {duenos/total_registros*100:.2f}% - Corredores {corredores/total_registros*100:.2f}%")
    
    out.append("\n==================================================================")
    out.append("SECCIÓN 2")
    out.append("=========")
    out.append(f"seller_profile_id presente: {profile_present}")
    out.append(f"seller_profile_id ausente: {profile_absent}")
    out.append(f"porcentaje presente: {profile_present/total_registros*100:.2f}%")

    out.append("\n==================================================================")
    out.append("SECCIÓN 3")
    out.append("=========")
    sospechosos_count = len(sospechosos_list)
    out.append(f"Corredores donde:")
    out.append(f"seller_profile_id existe")
    out.append(f"broker_brand = N/A")
    out.append(f"company_name = N/A")
    out.append(f"seller_is_pro = False")
    out.append(f"\nCantidad: {sospechosos_count}")
    pct_sospechosos = (sospechosos_count / corredores * 100) if corredores > 0 else 0
    out.append(f"Porcentaje sobre corredores: {pct_sospechosos:.2f}%")

    out.append("\n==================================================================")
    out.append("SECCIÓN 4")
    out.append("=========")
    out.append(f"Primeros 50 casos sospechosos:\n")
    for s in sospechosos_list[:50]:
        out.append(f"URL: {s['url']}")
        out.append(f"PUBLICADOR: {s['publicador']}")
        out.append(f"PROFILE_ID: {s['profile_id']}")
        out.append(f"BROKER_BRAND: {s['broker_brand']}")
        out.append(f"COMPANY_NAME: {s['company_name']}")
        out.append(f"SELLER_IS_PRO: {s['seller_is_pro']}")
        out.append(f"ES_PROPIETARIO_DIRECTO: {s['es_propietario_directo']}")
        out.append("---")

    out.append("\n==================================================================")
    out.append("SECCIÓN 5")
    out.append("=========")
    out.append(f"HTML Analizados de Hoy: {htmls_analizados}")
    if htmls_analizados > 0:
        out.append(f"HTML con /user/profile/id/: {html_with_profile}")
        out.append(f"HTML sin /user/profile/id/: {html_without_profile}")
        out.append(f"Porcentaje con profile_id: {html_with_profile/htmls_analizados*100:.2f}%")
    else:
        out.append("No se encontraron dumps de HTML descargados localmente.")

    out.append("\n==================================================================")
    out.append("SECCIÓN 6")
    out.append("=========")
    if htmls_analizados > 0:
        out.append(f"contact_logo: {html_contact_logo}")
        out.append(f"advertiser-avatar: {html_avatar}")
        out.append(f"badge profesional: {html_badge_pro}")
        out.append(f"profile_id: {html_profile_id}")

    out.append("\n==================================================================")
    out.append("SECCIÓN EXTRA")
    out.append("=============")
    out.append("TOP COMPANY_NAME\n")
    for name, count in company_names.most_common(20):
        out.append(f"{name} -> {count}")
        
    out.append("\nTOP BROKER_BRAND\n")
    for name, count in broker_brands.most_common(20):
        out.append(f"{name} -> {count}")

    out.append("\n==================================================================")
    out.append("SECCIÓN 7")
    out.append("=========")
    out.append("CONCLUSIONES AUTOMÁTICAS\n")
    
    if htmls_analizados > 0 and (html_with_profile / htmls_analizados) > 0.90:
        out.append("ALERTA:\nPROFILE_ID ES UNA CARACTERÍSTICA CASI UNIVERSAL.\n")
        
    if corredores > 0 and (sospechosos_count / corredores) > 0.80:
        out.append("ALERTA:\nLA CLASIFICACIÓN ESTÁ SIENDO DOMINADA POR PROFILE_ID.\n")

    report_text = "\n".join(out)
    
    print(report_text)
    
    with open("audit_today_report.txt", "w", encoding="utf-8") as f:
        f.write(report_text)

    client.close()

if __name__ == "__main__":
    asyncio.run(main())
