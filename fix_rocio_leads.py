import asyncio
import logging
from chatbot.storage import get_db
from chatbot.lead_router import get_executive_phone, format_whatsapp_template
from chatbot.notification_service import NotificationService
from chatbot.constants import CHILE_TZ
from datetime import datetime

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fix_rocio_leads")

async def fix_and_notify():
    db = get_db()
    
    # 1. Buscar leads con el nombre mal escrito "Rocio Aliaga" (sin acento)
    wrong_name = "Rocio Aliaga"
    correct_name = "Rocío Aliaga"
    
    leads = list(db["leads"].find({"ejecutivo_asignado": wrong_name}))
    logger.info(f"Encontrados {len(leads)} leads con nombre '{wrong_name}'")
    
    if not leads:
        logger.info("No hay leads pendientes de corrección.")
        return

    # Obtener el teléfono correcto de Rocío (usando la nueva lógica robusta)
    exec_phone = get_executive_phone(correct_name)
    if not exec_phone:
        logger.error(f"No se pudo encontrar el teléfono para '{correct_name}'. Abortando.")
        return

    logger.info(f"Teléfono encontrado para {correct_name}: {exec_phone}")

    for lead in leads:
        phone = lead.get("phone")
        property_code = lead.get("prospecto", {}).get("codigo") or lead.get("property_code")
        
        logger.info(f"Procesando lead {phone} para propiedad {property_code}...")

        # A. Actualizar nombre en la base de datos
        db["leads"].update_one(
            {"_id": lead["_id"]},
            {"$set": {
                "ejecutivo_asignado": correct_name,
                "prospecto.ejecutivo": correct_name
            }}
        )
        logger.info(f" - Nombre corregido en DB a '{correct_name}'")

        # B. Preparar y enviar notificación
        lead_data = {
            "phone": phone,
            "nombre": lead.get("prospecto", {}).get("nombre", "Cliente"),
            "last_message": "Consulta pendiente de notificación inicial.",
            "property_code": property_code
        }
        
        message = format_whatsapp_template(lead_data, correct_name, property_code, is_new_assignment=True)
        
        sent = await NotificationService.send_notification(
            phone=exec_phone,
            message=message,
            alert_type="MANUAL_FIX_NOTIFICATION",
            meta={"to": correct_name, "reason": "accent_fix_retrigger"}
        )
        
        if sent:
            logger.info(f" - Notificación enviada exitosamente a {correct_name}")
        else:
            logger.error(f" - Falló el envío de notificación para lead {phone}")

if __name__ == "__main__":
    asyncio.run(fix_and_notify())
