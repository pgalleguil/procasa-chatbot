import os
import sys
import asyncio
import unicodedata
import re
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from motor.motor_asyncio import AsyncIOMotorClient
from scraping.scraping_yapo_proxys import _BROKER_KEYWORDS

def normalize(text):
    if not text: return ""
    text = str(text).lower().strip()
    text = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', text)

async def main():
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]

    print("Calculando el total de dueños a auditar...")
    query = {"details.es_propietario_directo": True}
    total_duenos = await coll.count_documents(query)

    if total_duenos == 0:
        print("No se encontraron dueños en la base de datos.")
        return

    print(f"Auditando {total_duenos} propiedades marcadas como 'Dueño'...")

    # Estructuras para análisis
    # key: profile_id (o publicador si profile_id es N/A). value: lista de URLs
    profile_listings = defaultdict(list)
    
    nombres_sospechosos = []
    descripciones_sospechosas = []
    
    nombres_genericos = ["agente", "vendedor", "propietario", "asesor", "ejecutivo", "corredor", "dueño", "dueno"]
    
    # Palabras que a veces se escapan del filtro si están mal escritas o en contextos raros
    keywords_sospechosas = ["comision", "honorarios", "nuestra web", "visite nuestra pagina", "horario de oficina"]

    from tqdm import tqdm
    
    with tqdm(total=total_duenos, desc="Analizando", unit="prop") as pbar:
        last_id = None
        while True:
            q = query.copy()
            if last_id:
                q["_id"] = {"$gt": last_id}
                
            records = await coll.find(q).sort("_id", 1).limit(1000).to_list(length=1000)
            
            if not records:
                break
                
            for doc in records:
                details = doc.get("details", {})
                pub = details.get("publicador", "N/A")
                prof_id = details.get("seller_profile_id", "N/A")
                desc = details.get("descripcion", details.get("descripcion_corta", ""))
                url = doc.get("url", "N/A")
                
                # Agrupación por usuario
                group_key = f"{prof_id}::{pub}" if prof_id != "N/A" else pub
                profile_listings[group_key].append(url)
                
                pub_norm = normalize(pub)
                
                # Check nombres sospechosos
                if any(ng in pub_norm.split() for ng in nombres_genericos):
                    nombres_sospechosos.append((pub, url))
                    
                # Check palabras clave ocultas en descripciones que pasaron el filtro
                desc_norm = normalize(desc)
                hit = [k for k in keywords_sospechosas if k in desc_norm]
                if hit:
                    descripciones_sospechosas.append((pub, hit, url))
                
                pbar.update(1)
                
            last_id = records[-1]["_id"]

    # --- ANÁLISIS DE RESULTADOS ---
    
    # Filtrar usuarios con 3 o más propiedades
    multi_publishers = {k: v for k, v in profile_listings.items() if len(v) >= 3}
    
    # Ordenar por cantidad de propiedades
    multi_publishers_sorted = sorted(multi_publishers.items(), key=lambda x: len(x[1]), reverse=True)

    out = []
    out.append("==================================================================")
    out.append(f"AUDITORÍA DE FAKE OWNERS (Corredores Camuflados) - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    out.append("==================================================================")
    out.append(f"Total de propiedades marcadas como dueño analizadas: {total_duenos}")
    
    out.append("\n==================================================================")
    out.append("1. ALERTA MULTI-PUBLICADORES (Mismo Perfil con 3+ Propiedades)")
    out.append("==================================================================")
    out.append(f"Se encontraron {len(multi_publishers)} cuentas con 3 o más propiedades activas.")
    for key, urls in multi_publishers_sorted[:20]: # Mostrar top 20
        prof_id, pub = key.split("::") if "::" in key else ("N/A", key)
        out.append(f"-> Publicador: {pub} (Profile: {prof_id}) | Total Avisos: {len(urls)}")
        out.append(f"   Ejemplo URL: {urls[0]}")
        
    out.append("\n==================================================================")
    out.append("2. NOMBRES GENÉRICOS SOSPECHOSOS")
    out.append("==================================================================")
    out.append(f"Se encontraron {len(nombres_sospechosos)} casos.")
    for pub, url in nombres_sospechosos[:15]:
        out.append(f"-> Nombre: {pub} | URL: {url}")
        
    out.append("\n==================================================================")
    out.append("3. LENGUAJE DE CORREDOR EN DESCRIPCIÓN (Se escaparon del filtro)")
    out.append("==================================================================")
    out.append(f"Se encontraron {len(descripciones_sospechosas)} casos.")
    for pub, hits, url in descripciones_sospechosas[:15]:
        out.append(f"-> Publicador: {pub} | Palabras: {hits} | URL: {url}")

    out.append("\n==================================================================")
    out.append("CONCLUSIONES Y PRÓXIMOS PASOS")
    out.append("==================================================================")
    out.append("- Si la sección 1 está llena, significa que hay corredores usando perfiles anónimos de 'dueño'.")
    out.append("  Solución sugerida: En is_likely_broker(), si no podemos saber cuántas propiedades tiene,")
    out.append("  podríamos endurecer la revisión semántica. Otra opción es hacer un cruce post-scraping.")
    
    report = "\n".join(out)
    print(report)
    
    with open("audit_fake_owners_report.txt", "w", encoding="utf-8") as f:
        f.write(report)
        
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
