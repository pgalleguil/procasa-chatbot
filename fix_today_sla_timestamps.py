# fix_today_sla_timestamps.py
import os
import logging
from datetime import datetime
import pytz
from pymongo import MongoClient

# Configuración de logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuración de zona horaria
CHILE_TZ = pytz.timezone('Chile/Continental')

def fix_leads():
    # Intentar cargar config local
    try:
        from config import Config
        mongo_uri = Config.MONGO_URI
        db_name = Config.DB_NAME
    except ImportError:
        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        db_name = os.getenv("DB_NAME", "chatbot_db")
    
    client = MongoClient(mongo_uri)
    db = client[db_name]
    
    # Rango: Desde ayer a las 19:01 hasta hoy a las 08:59
    # Ayer fue 2026-02-18
    # Hoy es 2026-02-19
    
    start_search = "2026-02-18T19:01"
    end_search = "2026-02-19T08:59"
    
    # El valor al que queremos resetear: Hoy (Feb 19) a las 09:00 AM
    target_ts = datetime(2026, 2, 19, 9, 0, 0)
    target_ts = CHILE_TZ.localize(target_ts).isoformat()
    
    logger.info(f"Buscando leads asignados entre {start_search} y {end_search}...")
    
    query = {
        "lifecycle.assigned_at": {
            "$gte": start_search,
            "$lt": end_search
        }
    }
    
    leads_to_fix = list(db["leads"].find(query, {"phone": 1, "ejecutivo_asignado": 1, "lifecycle.assigned_at": 1}))
    
    if not leads_to_fix:
        logger.info("No se encontraron leads que requieran corrección.")
        return

    logger.info(f"Se encontraron {len(leads_to_fix)} leads para corregir.")
    
    count = 0
    for lead in leads_to_fix:
        phone = lead.get("phone")
        old_ts = lead.get("lifecycle", {}).get("assigned_at")
        exec_name = lead.get("ejecutivo_asignado")
        
        logger.info(f"Corrigiendo lead {phone} ({exec_name}): {old_ts} -> {target_ts}")
        
        result = db["leads"].update_one(
            {"_id": lead["_id"]},
            {"$set": {"lifecycle.assigned_at": target_ts}}
        )
        
        if result.modified_count > 0:
            count += 1
            
    logger.info(f"Proceso finalizado. Se actualizaron {count} leads.")

if __name__ == "__main__":
    fix_leads()
