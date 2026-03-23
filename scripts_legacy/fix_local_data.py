import os
import asyncio
import hashlib
from motor.motor_asyncio import AsyncIOMotorClient

from config import Config
from scraping_yapo_proxys import _parse_html_fast, parse_price_components, get_uf_value, clean_num, clean_float, generate_content_hash, is_likely_broker, normalize_text

async def main():
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]
    
    uf_value = await get_uf_value()
    print(f"UF from API: {uf_value}")
    
    html_dumps_dir = os.path.join(os.path.dirname(__file__), "html_dumps")
    
    cursor = coll.find({})
    updated_count = 0
    total_found = 0
    
    async for doc in cursor:
        url = doc.get("url")
        if not url: continue
        
        filename = hashlib.md5(url.encode()).hexdigest() + ".html"
        filepath = os.path.join(html_dumps_dir, filename)
        
        if os.path.exists(filepath):
            total_found += 1
            with open(filepath, "r", encoding="utf-8") as f:
                html = f.read()
                
            raw_data = _parse_html_fast(html)
            if not raw_data: 
                print(f"[ERROR] Corrupted HTML detected. Deleting backup and resetting DB for: {url}")
                os.remove(filepath)
                await coll.delete_one({"_id": doc["_id"]})
                queue_coll = db["yapo_queue"]
                await queue_coll.update_one({"url": url}, {"$set": {"status": "pending", "retries": 0}})
                continue
            
            # Recalculate price fields
            price = raw_data.get("price", "N/A")
            p_uf, p_clp = parse_price_components(price)
            if not p_uf and p_clp and uf_value: 
                p_uf = round(p_clp / uf_value, 2)
            elif not p_clp and p_uf and uf_value: 
                p_clp = int(p_uf * uf_value)
                
            # Get coords
            lat = raw_data.get("lat", "N/A")
            lon = raw_data.get("lon", "N/A")
            
            # Additional metrics for Mobile layout
            banos_str = raw_data.get("banos_str", "N/A")
            estacionamientos_str = raw_data.get("estacionamientos_str", "N/A")
            dormitorios_str = raw_data.get("dormitorios_str", "N/A")
            
            # Get m2 total to recalculate UF/m2
            details = doc.get("details", {})
            m2_tot = details.get("m2_total")
            if m2_tot is None:
                m2_tot_str = raw_data.get("m2_total_str", "N/A")
                m2_tot = clean_float(m2_tot_str)
                
            p_uf_m2 = None
            if p_uf and m2_tot and m2_tot > 0:
                p_uf_m2 = round(float(p_uf) / float(m2_tot), 3)
                
            update_fields = {}
            if lat != "N/A": 
                update_fields["details.lat"] = lat
            if lon != "N/A":
                update_fields["details.lon"] = lon
            
            if banos_str != "N/A":
                update_fields["details.banos"] = clean_num(banos_str)
            if estacionamientos_str != "N/A":
                update_fields["details.estacionamientos"] = clean_num(estacionamientos_str)
            if dormitorios_str != "N/A":
                update_fields["details.dormitorios"] = clean_num(dormitorios_str)
                
            update_fields["details.precio"] = price
            update_fields["details.precio_uf"] = p_uf
            update_fields["details.precio_clp_raw"] = p_clp
            update_fields["details.m2_total"] = m2_tot
            if p_uf_m2 is not None:
                update_fields["details.precio_uf_m2"] = p_uf_m2
            # Get full description
            desc_corta = raw_data.get("raw_desc")
            if desc_corta and desc_corta != "N/A":
                update_fields["details.descripcion"] = normalize_text(desc_corta, 8000)
                update_fields["details.content_hash"] = generate_content_hash(doc.get("details", {}).get("titulo", ""), desc_corta)
                
                # Re-evaluate broker status using full description
                publicador = raw_data.get("publicador", "N/A")
                company_name = raw_data.get("company_name", "N/A")
                is_broker = is_likely_broker(publicador, desc_corta, company_name)
                update_fields["details.es_propietario_directo"] = not is_broker
                update_fields["details.confianza_propietario"] = 1.0 if is_broker else 0.95
                
                # Also save the newly extracted names
                if publicador and publicador != "N/A":
                    update_fields["details.publicador"] = publicador
                    update_fields["details.nombre_ejecutivo"] = publicador
                if company_name and company_name != "N/A":
                    update_fields["details.company_name"] = company_name
                    update_fields["details.nombre_corredora"] = company_name

            await coll.update_one({"_id": doc["_id"]}, {"$set": update_fields})
            updated_count += 1
            
            if updated_count % 50 == 0:
                print(f"Updated {updated_count} records so far...")

    print(f"\nCompleted! Found local HTMLs for {total_found} properties. Updated {updated_count} records in DB.")

if __name__ == '__main__':
    asyncio.run(main())
