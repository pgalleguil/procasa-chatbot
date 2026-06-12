import asyncio
import os
import hashlib
from motor.motor_asyncio import AsyncIOMotorClient
import re
import argparse

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

def _clean(s: str) -> str:
    if not s:
        return "N/A"
    s = re.sub(r'<[^>]+>', '', s).strip()
    s = re.sub(r'\s+', ' ', s)
    return s if s and "Pregunta al anunciante" not in s else "N/A"

def extract_new_signals(html: str):
    broker_brand = "N/A"
    seller_profile_id = "N/A"
    
    # contact_logo
    contact_logo_match = re.search(r'class="[^"]*contact_logo[^"]*"[^>]*>\s*<img[^>]+alt="([^"]+)"', html, re.IGNORECASE)
    if contact_logo_match:
        c_val = _clean(contact_logo_match.group(1))
        generic_avatars = ["user-avatar", "avatar", "default-user"]
        if c_val and c_val != "N/A" and not any(g in c_val.lower() for g in generic_avatars):
            broker_brand = c_val
            
    # profile id
    profile_id_match = re.search(r'href="[^"]*/user/profile/id/(\d+)[^"]*"', html, re.IGNORECASE)
    if profile_id_match:
        seller_profile_id = profile_id_match.group(1)
        
    # avatar
    if broker_brand == "N/A":
        avatar_match = re.search(r'class="[^"]*advertiser-avatar[^"]*"[^>]*>\s*<img[^>]+alt="([^"]+)"', html, re.IGNORECASE)
        if avatar_match:
            c_val = _clean(avatar_match.group(1))
            generic_avatars = ["user-avatar", "avatar", "default-user"]
            if c_val and c_val != "N/A" and not any(g in c_val.lower() for g in generic_avatars):
                broker_brand = c_val

    return broker_brand, seller_profile_id

async def run_migration(dry_run=False):
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]
    
    html_dumps_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "html_dumps")
    
    # Priorizar dueños directos (los candidatos a ser falsos dueños)
    # Las corredoras ya clasificadas (es_propietario_directo: False) no necesitan reprocesarse
    query = {"details.es_propietario_directo": True}
    cursor = coll.find(query)
    if dry_run:
        cursor = cursor.limit(100)
        
    analizadas = 0
    reclasificadas = 0
    errores = 0
    
    async for doc in cursor:
        url = doc.get("url")
        if not url:
            continue
            
        filename = hashlib.md5(url.encode()).hexdigest() + ".html"
        filepath = os.path.join(html_dumps_dir, filename)
        
        if not os.path.exists(filepath):
            continue
            
        analizadas += 1
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                html = f.read()
                
            bb, spi = extract_new_signals(html)
            
            updates = {
                "html_version": 1,
                "parsed_version": 3
            }
            
            needs_update = False
            reclasificado = False
            
            if bb != "N/A":
                updates["details.nombre_corredora"] = bb
                updates["details.company_name"] = bb
                if doc.get("details", {}).get("es_propietario_directo", False) is True:
                    updates["details.es_propietario_directo"] = False
                    updates["details.confianza_propietario"] = 1.0
                    reclasificado = True
                needs_update = True
                
            if spi != "N/A":
                updates["details.vendedor_id"] = spi
                needs_update = True
                
            if needs_update or True: # always update versions
                if not dry_run:
                    await coll.update_one({"_id": doc["_id"]}, {"$set": updates})
                    
            if reclasificado:
                reclasificadas += 1
                
        except Exception as e:
            errores += 1
            
    if dry_run:
        print("--- DRY RUN COMPLETED ---")
    else:
        print("--- FULL MIGRATION COMPLETED ---")
        
    print(f"Analizadas (con HTML): {analizadas}")
    print(f"Reclasificadas (dueño -> corredora): {reclasificadas}")
    print(f"Errores encontrados: {errores}")
    
    client.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_migration(args.dry_run))
