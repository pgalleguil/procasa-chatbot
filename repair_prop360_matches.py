import os
import sys
import asyncio
import time
import argparse
from datetime import datetime
from pymongo import MongoClient
from tqdm import tqdm
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

sys.path.append(os.path.join(os.path.dirname(__file__), ""))
from config import Config
from scraping_prop360_portales import scrape_portal_info, LOGIN_URL, PROPERTIES_URL, USERNAME, PASSWORD

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Límite de propiedades")
    args = parser.parse_args()

    start_time = time.time()
    
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    coll = db["universo_cartera"]

    # 1. CREACIÓN DEL BACKUP MONGO
    backup_col_name = f"universo_cartera_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"\n--- PASO 1: BACKUP EN MONGODB ---")
    print(f"Creando colección de respaldo: {backup_col_name}")
    
    # Realizar backup completo nativo en Mongo
    coll.aggregate([
        {"$match": {}},
        {"$out": backup_col_name}
    ])
    
    backup_coll = db[backup_col_name]
    
    # Replicar índices
    for index in coll.list_indexes():
        idx_keys = [(k, v) for k, v in index["key"].items()]
        if idx_keys == [("_id", 1)]: continue
        kwargs = {"name": index.get("name"), "unique": index.get("unique", False)}
        try:
            backup_coll.create_index(idx_keys, **kwargs)
        except Exception as e:
            print(f"Advertencia: no se pudo replicar el índice {index.get('name')}: {e}")
    
    # 2. VALIDACIÓN DEL BACKUP
    original_count = coll.count_documents({})
    backup_count = backup_coll.count_documents({})
    
    print("\n--- PASO 2: VALIDACIÓN DE RESPALDO ---")
    print(f"Documentos en original: {original_count}")
    print(f"Documentos en backup  : {backup_count}")
    
    if original_count != backup_count or original_count == 0:
        print("\n[CRÍTICO] Fallo en la validación del backup. Abortando reparación por seguridad.")
        sys.exit(1)
        
    print("[OK] Backup validado exitosamente. Procediendo a reparación.\n")

    # 3. EJECUCIÓN DE LA REPARACIÓN
    # Obtenemos propiedades de la oficina SUCRE que estén disponibles para re-scrapear
    docs = list(coll.find({"oficina": "PROCASA SUCRE", "disponible": True}))
    if args.limit:
        docs = docs[:args.limit]
    
    print(f"--- PASO 3: RE-SCRAPING Y REPARACIÓN ---")
    print(f"Total de propiedades a revisar en Prop360: {len(docs)}")
    
    report = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        
        print("Iniciando sesión en Prop360...")
        await page.goto(LOGIN_URL)
        await page.fill("#tbMail", USERNAME)
        await page.fill("#tbPassword", PASSWORD)
        await page.keyboard.press("Enter")
        
        try:
            await page.wait_for_url("**/backoffice/**", timeout=15000)
        except:
            await page.click("#btnIngresar, .btn-login, button:has-text('Ingresar')")
            await page.wait_for_url("**/backoffice/**", timeout=20000)
            
        await page.goto(PROPERTIES_URL)

        for doc in tqdm(docs, desc="Reparando propiedades"):
            code = doc["codigo"]
            
            try:
                scraped_data = await scrape_portal_info(page, code)
            except Exception as e:
                tqdm.write(f"Error scraper en {code}: {e}")
                scraped_data = None

            status = "Error/No Encontrado"
            if scraped_data:
                existing_pub = doc.get("publicaciones", {})
                if not isinstance(existing_pub, dict): existing_pub = {}
                
                scraped_codigo_internacional = scraped_data.get("codigo_internacional")
                existing_codigo_internacional = existing_pub.get("codigo_internacional")
                
                # Armamos los nuevos datos de publicaciones respetando lo existente
                new_pub = existing_pub.copy()
                if scraped_codigo_internacional:
                    new_pub["codigo_internacional"] = scraped_codigo_internacional
                
                scraping_pubs = scraped_data.get("publicaciones_scraping", {})
                for p_key, p_data in scraping_pubs.items():
                    if p_key not in new_pub or not isinstance(new_pub[p_key], dict):
                        new_pub[p_key] = {}
                    new_pub[p_key].update(p_data)
                
                # Checkeamos discrepancia (si los datos actuales son distintos a los obtenidos)
                has_discrepancy = False
                if str(scraped_codigo_internacional) != str(existing_codigo_internacional):
                    has_discrepancy = True
                
                # Para mayor robustez, forzamos actualización si cambian links o falta info
                if new_pub != existing_pub or has_discrepancy:
                    update_fields = {
                        "publicaciones": new_pub,
                        "ultima_actualizacion_scraping": datetime.now().isoformat()
                    }
                    
                    if scraped_codigo_internacional:
                        update_fields["codigo_internacional"] = scraped_codigo_internacional
                        
                    historial = list(doc.get("historial_cambios", []))
                    
                    skip_historial = False
                    if historial and historial[-1].get("campo") == "reparacion_scraping":
                        skip_historial = True
                        
                    if not skip_historial:
                        historial.append({
                            "fecha": datetime.now().isoformat(),
                            "campo": "reparacion_scraping",
                            "valor_anterior": existing_codigo_internacional,
                            "valor_nuevo": scraped_codigo_internacional
                        })
                        update_fields["historial_cambios"] = historial
                    
                    tqdm.write(f"\n[REPARACIÓN] Modificando código {code} con los siguientes campos ($set):")
                    tqdm.write(str(update_fields))
                    
                    # Usar exclusivamente $set para preservar todo lo demás (embeddings, campañas, etc.)
                    coll.update_one({"codigo": code}, {"$set": update_fields})
                    status = "Corregido"
                else:
                    status = "Sin Cambios"
                    
            report.append({
                "codigo": code,
                "status": status
            })

        await browser.close()
        
    # 4. REPORTE FINAL
    end_time = time.time()
    total_minutes = round((end_time - start_time) / 60, 2)
    
    revisadas = len(report)
    corregidas = sum(1 for r in report if r["status"] == "Corregido")
    sin_cambios = sum(1 for r in report if r["status"] == "Sin Cambios")
    con_error = sum(1 for r in report if r["status"] == "Error/No Encontrado")
    
    print(f"\n--- PASO 4: REPORTE FINAL ---")
    print(f"Tiempo Total de Ejecución : {total_minutes} minutos")
    print(f"Colección Backup Creada   : {backup_col_name}")
    print(f"Propiedades Revisadas     : {revisadas}")
    print(f"Propiedades Corregidas    : {corregidas}")
    print(f"Propiedades Sin Cambios   : {sin_cambios}")
    print(f"Propiedades Con Error     : {con_error}")

if __name__ == "__main__":
    asyncio.run(main())
