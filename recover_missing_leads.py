# recover_missing_leads.py
import os
import sys
import logging
import json
import re
from datetime import datetime, timedelta
from pymongo import MongoClient

# Setup paths
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import Config
from chatbot.storage import get_db
from chatbot.link_extractor import analizar_mensaje_para_link, extraer_codigo_internacional
from chatbot.lead_router import find_responsible_executive
from chatbot.crm_service import CrmService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("recovery")

def recover_leads(dry_run=True):
    db = get_db()
    
    # 1. Buscar alertas de propiedad faltante recientes (últimos 7 días)
    limit_date = datetime.now() - timedelta(days=7)
    query_notifications = {
        "lead_data.lead_type": "MISSING_PROPERTY_ALERT",
        "created_at": {"$gte": limit_date.isoformat() if isinstance(limit_date.isoformat(), str) else limit_date}
    }
    
    alerts = list(db["pending_notifications"].find(query_notifications).sort("created_at", -1))
    logger.info(f"Encontradas {len(alerts)} alertas de 'Missing Property' en los últimos 7 días.")

    results = []
    processed_phones = set()

    for alert in alerts:
        lead_data = alert.get("lead_data", {})
        # Extraer el teléfono del cliente del campo last_message si no está directo
        # Ejemplo: "...del cliente Luis (+56992506799)..."
        last_msg = lead_data.get("last_message", "")
        phone_match = re.search(r"\(\+(\d+)\)", last_msg)
        if not phone_match:
            continue
        
        phone = "+" + phone_match.group(1)
        if phone in processed_phones:
            continue
        processed_phones.add(phone)

        logger.info(f"--- Procesando Lead: {phone} ---")
        
        # 2. Buscar el lead en la BD
        lead = db["leads"].find_one({"phone": phone})
        if not lead:
            logger.warning(f"Lead {phone} no encontrado en la colección 'leads'.")
            continue

        # 3. Re-analizar historial
        messages = lead.get("messages", [])
        if not messages:
            logger.warning(f"Lead {phone} no tiene historial de mensajes.")
            continue
            
        all_text = " ".join([m.get("content", "") for m in messages[-5:]])
        
        found_link, prop_match, platform, code_raw = analizar_mensaje_para_link(all_text)
        
        final_code = None
        if found_link and prop_match:
            final_code = str(prop_match.get("codigo"))
            logger.info(f"¡Match Encontrado! Propiedad: {final_code} ({platform})")
        else:
            # Probar código internacional
            c_int = extraer_codigo_internacional(all_text)
            if c_int:
                prop_int = db["universo_cartera"].find_one({
                    "$or": [
                        {"codigo_internacional": c_int},
                        {"publicaciones.codigo_internacional": c_int}
                    ]
                })
                if prop_int:
                    final_code = str(prop_int.get("codigo"))
                    logger.info(f"¡Match por Cód Internacional! Propiedad: {final_code}")

        if final_code:
            # 4. Determinar nuevo ejecutivo
            exec_name, exec_phone, method = find_responsible_executive(
                property_code=final_code,
                lead_phone=phone,
                lead_name=lead.get("prospecto", {}).get("nombre") or lead.get("nombre")
            )
            
            if exec_name and exec_name != "No Asignado":
                logger.info(f"Propuesta de asignación: {exec_name} ({exec_phone})")
                results.append({
                    "phone": phone,
                    "old_code": lead.get("prospecto", {}).get("codigo") or "N/D",
                    "new_code": final_code,
                    "new_executive": exec_name,
                    "lead_id": lead["_id"]
                })
                
                if not dry_run:
                    # Aplicar cambios
                    db["leads"].update_one(
                        {"_id": lead["_id"]},
                        {"$set": {
                            "prospecto.codigo": final_code,
                            "ejecutivo_asignado": exec_name,
                            "prospecto.ejecutivo": exec_name,
                            "auto_recovered": True,
                            "updated_at": datetime.now().isoformat()
                        }}
                    )
                    # Opcional: Podríamos re-enviar la notificación, pero por ahora solo actualizamos el CRM
                    logger.info(f"Lead {phone} actualizado en la base de datos.")
        else:
            logger.info(f"No se pudo identificar propiedad para {phone} aún.")

    return results

if __name__ == "__main__":
    is_dry = "--commit" not in sys.argv
    print(f"\n--- INICIANDO PROCESO DE RECUPERACIÓN (Dry Run: {is_dry}) ---")
    
    recovered = recover_leads(dry_run=is_dry)
    
    print("\n" + "="*50)
    print(f"RESULTADOS DE RECUPERACIÓN: {len(recovered)} leads identificados")
    print("="*50)
    for r in recovered:
        print(f"Lead: {r['phone']} | Código: {r['old_code']} -> {r['new_code']} | Ejecutivo: {r['new_executive']}")
    print("="*50)
    
    if is_dry and recovered:
        print("\nPara aplicar los cambios, ejecuta: python recover_missing_leads.py --commit")
    elif not recovered:
        print("\nNo se encontraron leads recuperables en este momento. Es posible que el scraper aún no haya procesado esas propiedades.")
