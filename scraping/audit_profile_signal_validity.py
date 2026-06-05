import os
import sys
import asyncio
import hashlib
import random
import unicodedata
from collections import Counter
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from motor.motor_asyncio import AsyncIOMotorClient

def is_natural_person(name):
    if not name or name == "N/A": return False
    words = name.strip().split()
    if len(words) < 2: return False
    
    text = str(name).lower().strip()
    text = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    
    enterprise_words = [
        "propiedades", "inmobiliaria", "corretaje", "corredores", 
        "re/max", "remax", "houm", "procasa", "exp", "fuenzalida", "nexxos", "brokers"
    ]
    if any(ew in text for ew in enterprise_words):
        return False
        
    return True

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
        with open("audit_profile_signal_validity.txt", "w", encoding="utf-8") as f:
            f.write(msg)
        return

    html_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "html_dumps")
    
    print("Iniciando auditoría definitiva de profile_id...")

    duenos = 0
    corredores = 0
    
    casos_exclusivos_profile = []
    
    for idx, doc in enumerate(records, 1):
        if idx % 50 == 0 or idx == total_registros:
            print(f"Procesando {idx}/{total_registros} ({(idx/total_registros)*100:.1f}%)", end="\r", flush=True)
            
        details = doc.get("details", {})
        es_dueno = details.get("es_propietario_directo", False)
        
        if es_dueno:
            duenos += 1
        else:
            corredores += 1
            
            pub = details.get("publicador", "N/A")
            comp = details.get("company_name", "N/A")
            brok = details.get("broker_brand", "N/A")
            prof_id = details.get("seller_profile_id", "N/A")
            is_pro = details.get("seller_is_pro", False)
            
            has_prof = prof_id != "N/A" and prof_id is not None and prof_id != ""
            has_pro = is_pro == True
            
            # Criterios Sec 2: profile y pro existen, pero no hay company ni broker
            if has_prof and has_pro and (brok == "N/A" or brok == "") and (comp == "N/A" or comp == ""):
                casos_exclusivos_profile.append({
                    "url": doc.get("url", "N/A"),
                    "publicador": pub,
                    "company_name": comp,
                    "broker_brand": brok,
                    "seller_profile_id": prof_id,
                    "seller_is_pro": is_pro,
                    "es_propietario_directo": es_dueno
                })

    print() # Salto de línea
    
    total_exclusivos = len(casos_exclusivos_profile)
    pct_exclusivos = (total_exclusivos / corredores * 100) if corredores > 0 else 0
    
    # Muestra de 100
    if total_exclusivos > 100:
        muestra = random.sample(casos_exclusivos_profile, 100)
    else:
        muestra = casos_exclusivos_profile
        
    html_stats = {
        "/user/profile/id/": 0,
        "contact_logo": 0,
        "advertiser-avatar": 0,
        "profesional_badge": 0,
        "analizados": 0
    }
    
    for c in muestra:
        url = c["url"]
        if url:
            filename = hashlib.md5(url.encode()).hexdigest() + ".html"
            html_path = os.path.join(html_dir, filename)
            if os.path.exists(html_path):
                try:
                    with open(html_path, "r", encoding="utf-8") as f:
                        html = f.read()
                    
                    html_stats["analizados"] += 1
                    if "/user/profile/id/" in html: html_stats["/user/profile/id/"] += 1
                    if "contact_logo" in html: html_stats["contact_logo"] += 1
                    if "advertiser-avatar" in html: html_stats["advertiser-avatar"] += 1
                    if 'title="Profesional"' in html or 'title=\'Profesional\'' in html or 'Profesional<' in html: 
                        html_stats["profesional_badge"] += 1
                except:
                    pass

    personas_naturales = 0
    top_publicadores = Counter()
    
    for c in casos_exclusivos_profile:
        pub = c["publicador"]
        if is_natural_person(pub):
            personas_naturales += 1
        top_publicadores[pub] += 1
        
    pct_naturales = (personas_naturales / total_exclusivos * 100) if total_exclusivos > 0 else 0

    out = []
    out.append("==================================================================")
    out.append("SECCIÓN 1: PANORAMA GENERAL")
    out.append("==================================================================")
    out.append(f"TOTAL ANALIZADOS (Ayer y Hoy): {total_registros}")
    out.append(f"DUEÑOS: {duenos}")
    out.append(f"CORREDORES: {corredores}")
    
    out.append("\n==================================================================")
    out.append("SECCIÓN 2: CORREDORES EXCLUSIVOS POR PROFILE_ID")
    out.append("==================================================================")
    out.append(f"Corredores donde (seller_profile_id EXISTE) Y (seller_is_pro = True)")
    out.append(f"PERO (broker_brand = N/A) Y (company_name = N/A)")
    out.append(f"\nCANTIDAD: {total_exclusivos}")
    out.append(f"PORCENTAJE SOBRE CORREDORES: {pct_exclusivos:.1f}%")
    
    out.append("\n==================================================================")
    out.append("SECCIÓN 3: MUESTRA ALEATORIA (100 CASOS)")
    out.append("==================================================================")
    for m in muestra:
        out.append(f"URL: {m['url']}")
        out.append(f"PUBLICADOR: {m['publicador']}")
        out.append(f"SELLER_PROFILE_ID: {m['seller_profile_id']}")
        out.append(f"SELLER_IS_PRO: {m['seller_is_pro']}")
        out.append(f"BROKER_BRAND: {m['broker_brand']}")
        out.append(f"COMPANY_NAME: {m['company_name']}")
        out.append(f"ES_PROPIETARIO_DIRECTO: {m['es_propietario_directo']}")
        out.append("---")
        
    out.append("\n==================================================================")
    out.append("SECCIÓN 4: AUDITORÍA HTML DE LA MUESTRA")
    out.append("==================================================================")
    out.append(f"Archivos HTML Encontrados y Leídos: {html_stats['analizados']} de {len(muestra)}")
    if html_stats['analizados'] > 0:
        out.append(f"/user/profile/id/ existe en: {html_stats['/user/profile/id/']}")
        out.append(f"contact_logo existe en: {html_stats['contact_logo']}")
        out.append(f"advertiser-avatar existe en: {html_stats['advertiser-avatar']}")
        out.append(f"Badge 'Profesional' existe en: {html_stats['profesional_badge']}")
        
    out.append("\n==================================================================")
    out.append("SECCIÓN 5: ANÁLISIS DE PERSONA NATURAL")
    out.append("==================================================================")
    out.append("Dentro de los casos detectados exclusivamente por PROFILE_ID:")
    out.append(f"Publicadores que parecen Persona Natural: {personas_naturales}")
    out.append(f"Porcentaje de Personas Naturales afectadas: {pct_naturales:.1f}%")

    out.append("\n==================================================================")
    out.append("SECCIÓN 6: TOP 100 PUBLICADORES (EXCLUSIVOS PROFILE_ID)")
    out.append("==================================================================")
    for name, count in top_publicadores.most_common(100):
        out.append(f"{name} -> {count}")

    out.append("\n==================================================================")
    out.append("SECCIÓN 7: CONCLUSIÓN AUTOMÁTICA")
    out.append("==================================================================")
    out.append("1. ¿seller_profile_id aparece en casi todos los avisos de Yapo?")
    if html_stats['analizados'] > 0 and (html_stats['/user/profile/id/'] / html_stats['analizados']) > 0.8:
        out.append("-> SÍ. La ruta /user/profile/id/ parece ser universal para dueños y corredores.")
    else:
        out.append("-> NO. La ruta no es universal en los HTML revisados.")
        
    out.append("\n2. ¿seller_is_pro está siendo derivado automáticamente desde seller_profile_id?")
    if total_exclusivos > 0 and html_stats['analizados'] > 0 and (html_stats['profesional_badge'] / html_stats['analizados']) < 0.2:
        out.append("-> MUY PROBABLE. Casi ningún HTML tiene el badge 'Profesional' visual real, pero seller_is_pro es True en la base de datos.")
    else:
        out.append("-> RESULTADO MIXTO. Revisar la lógica de extracción de seller_is_pro.")
        
    out.append("\n3. ¿Cuántos corredores dependen exclusivamente de estas señales?")
    out.append(f"-> {total_exclusivos} corredores ({pct_exclusivos:.1f}% del total de corredores).")
    
    out.append("\n4. ¿Existe evidencia estadística de que profile_id está sobreclasificando personas naturales?")
    if pct_naturales > 70:
        out.append(f"-> SÍ, EVIDENCIA CRÍTICA. El {pct_naturales:.1f}% de los casos clasificados exclusivamente por profile_id/pro son personas naturales sin rastro de empresa.")
    else:
        out.append(f"-> EVIDENCIA MODERADA. El {pct_naturales:.1f}% parecen personas naturales.")

    report = "\n".join(out)
    print(report)
    with open("audit_profile_signal_validity.txt", "w", encoding="utf-8") as f:
        f.write(report)
        
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
