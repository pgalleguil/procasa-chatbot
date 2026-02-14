import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from .storage import get_db, log_event
from .constants import CHILE_TZ, EventType
from .whatsapp_client import send_whatsapp_message

logger = logging.getLogger(__name__)

class NotificationService:
    """
    Servicio centralizado para envío de notificaciones con idempotencia.
    Evita duplicados verificando el historial en 'crm_events'.
    """

    @staticmethod
    async def send_notification(
        phone: str, 
        message: str, 
        alert_type: str, 
        meta: Optional[Dict[str, Any]] = None,
        dedup_window_minutes: int = 5
    ) -> bool:
        """
        Envía una notificación de WhatsApp asegurando no duplicar envíos recientes.
        
        Args:
            phone: Número de destino.
            message: Contenido del mensaje.
            alert_type: Tipo de alerta (ej: 'NEW_LEAD', 'SLA_WARNING', 'ASSIGNMENT').
            meta: Metadatos adicionales para el log.
            dedup_window_minutes: Ventana de tiempo para considerar un mensaje como duplicado.
        
        Returns:
            bool: True si se envió (o ya estaba enviado recientemente), False si falló.
        """
        try:
            db = get_db()
            phone_clean = phone.replace("+", "").replace(" ", "").strip()
            
            # 1. VERIFICACIÓN DE IDEMPOTENCIA (Anti-Rebote)
            # Buscamos si ya se envió un evento de este tipo a este número recientemente.
            window_start = datetime.now(CHILE_TZ) - timedelta(minutes=dedup_window_minutes)
            
            existing_event = db["crm_events"].find_one({
                "phone": phone_clean,
                "type": "ALERT_SENT",
                "meta.type": alert_type,
                "timestamp": {"$gte": window_start.isoformat()}
            })
            
            if existing_event:
                logger.warning(f"[NOTIFICATION] Bloqueado duplicado '{alert_type}' para {phone_clean}. Enviado previamente a las {existing_event.get('timestamp')}")
                return True # Retornamos True para que el caller asuma "éxito" (ya fue gestionado)

            # 2. INTENTO DE ENVÍO
            sent = await send_whatsapp_message(phone, message)
            
            if sent:
                # 3. LOG DE ÉXITO (La fuente de la verdad para futuras deduplicaciones)
                log_meta = meta or {}
                log_meta["type"] = alert_type # Clave para la búsqueda futura
                log_meta["content_snippet"] = message[:50]
                
                log_event(
                    phone=phone_clean,
                    event_type=EventType.ALERT_SENT, # Usamos el tipo estándar
                    actor="system",
                    meta=log_meta
                )
                logger.info(f"[NOTIFICATION] Enviado '{alert_type}' a {phone_clean}")
                return True
            else:
                logger.error(f"[NOTIFICATION] Falló envío '{alert_type}' a {phone_clean}")
                return False

        except Exception as e:
            logger.error(f"[NOTIFICATION] Error crítico enviando notificación: {e}", exc_info=True)
            return False
