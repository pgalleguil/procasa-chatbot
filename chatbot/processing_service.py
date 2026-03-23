# chatbot/processing_service.py
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from .constants import CHILE_TZ, UNASSIGNED_LABEL
from .lead_router import find_responsible_executive
from .storage import get_db, save_pending_notification
from api_captacion import (
    get_zone_for_comuna, normalize_commune_v2,
    _normalize_tipo, _normalize_operacion
)
from bson import ObjectId

logger = logging.getLogger(__name__)

class LeadProcessingService:
    @staticmethod
    def _db():
        return get_db()

    @staticmethod
    def _normalize_commune_ui(name: str) -> str:
        """
        Normalización que mantiene acentos y Ñ para coincidir con el Matching Engine del UI.
        """
        if not name: return "DESCONOCIDA"
        c = str(name).lower().strip().replace("-", " ").replace("_", " ")
        c = " ".join(c.split())
        # Mapeo de sinónimos comunes sin quitar acentos
        mapping = {
            "stgo": "santiago",
            "santiago centro": "santiago",
            "nunoa": "ñuñoa",
            "penalolen": "peñalolén",
            "peñalolen": "peñalolén",
            "vina del mar": "viña del mar"
        }
        c = mapping.get(c, c)
        return c.upper()

    @staticmethod
    def classify(lead_doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calcula cluster_id y zone para un lead, buscando datos en universo_obelix si es necesario.
        """
        prospecto = lead_doc.get("prospecto", {}) or {}
        
        # 1. Obtener datos base
        comuna = prospecto.get("comuna") or lead_doc.get("comuna_interes")
        tipo = prospecto.get("tipo") or lead_doc.get("tipo_interes") or lead_doc.get("tipo_propiedad")
        operacion = prospecto.get("operacion") or lead_doc.get("operacion")
        
        # 2. Si falta comuna pero tenemos código, buscar en universo_obelix
        property_code = prospecto.get("codigo") or lead_doc.get("codigo")
        if not comuna and property_code:
            try:
                db = LeadProcessingService._db()
                p_code_int = int(property_code) if str(property_code).isdigit() else None
                prop = db["universo_obelix"].find_one({"codigo": p_code_int}) if p_code_int else None
                if not prop:
                    prop = db["universo_obelix"].find_one({"codigo": str(property_code)})
                
                if prop:
                    details = prop.get("details", {})
                    # Usar la comuna del scraping que suele tener acentos
                    comuna = prop.get("comuna") or details.get("comuna")
                    tipo = tipo or prop.get("tipo") or details.get("tipo_propiedad")
                    operacion = operacion or prop.get("operacion") or details.get("operacion")
            except Exception as e:
                logger.warning(f"[PROCESS_SERVICE] Error buscando prop {property_code} para clasificar: {e}")

        # 3. Normalizar y generar Cluster IDs
        comuna_ui = LeadProcessingService._normalize_commune_ui(comuna)
        tipo_code = _normalize_tipo(tipo)
        op_code = _normalize_operacion(operacion)
        
        cluster_id = f"{comuna_ui}-{tipo_code}-{op_code}"
        zone = get_zone_for_comuna(comuna) if comuna else "unknown"
        
        return {
            "cluster_id": cluster_id,
            "zone": zone,
            "operacion": op_code,
            "tipo": tipo_code,
            "comuna": comuna # Root field for matching
        }

    @staticmethod
    def is_worthy_of_assignment(lead_doc: Dict[str, Any]) -> bool:
        """
        Determina si un lead merece ser asignado a un ejecutivo humano de forma PROACTIVA
        durante el proceso de reparación de datos.
        
        IMPORTANTE: Solo asignamos si hay una señal EXPLÍCITA de intención que el chatbot
        ya detectó pero no pudo asignar por falta de datos.
        """
        # 1. SOLO INTENCIÓN EXPLÍCITA CONFIRMADA POR EL BOT
        # No asignamos por recencia ni por mensajes pendientes, dejamos que el bot lo maneje
        # cuando el cliente vuelva a hablar.
        
        high_intent_types = ["ASK_VISIT", "GIVE_OFFER", "VISITA_SOLICITADA", "CONTACTO_HUMANO"]
        
        # Revisar BI Analytics (Señal más fuerte del Bot)
        bi_res = lead_doc.get("bi_analytics_global", {}).get("RESULTADO_CHAT")
        if bi_res in high_intent_types:
            return True

        # Revisar si se intentó enviar una alerta de alta intención
        prospecto = lead_doc.get("prospecto", {}) or {}
        alerts = prospecto.get("alerts_sent", {})
        if "InteresVisita" in alerts or "SolicitudContacto" in alerts:
            return True
            
        # 2. DEFAULT: No asignamos. 
        # Al reparar cluster/zone, el bot podrá asignar solo la próxima vez que el cliente hable.
        return False

    @staticmethod
    def reassign_if_needed(lead_doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Intenta asignar un ejecutivo si el lead está "No Asignado".
        """
        prospecto = lead_doc.get("prospecto", {}) or {}
        current_exec = lead_doc.get("ejecutivo_asignado") or prospecto.get("ejecutivo")
        
        unassigned_labels = [UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", None, ""]
        
        if current_exec not in unassigned_labels:
            return {} # Ya tiene asignado

        # NUEVO: Solo asignar si el lead tiene "mérito" (intención o novedad)
        if not LeadProcessingService.is_worthy_of_assignment(lead_doc):
            logger.info(f"[PROCESS_SERVICE] Lead {lead_doc.get('phone')} descartado para auto-asignación (Baja intención/Histórico)")
            return {}

        property_code = prospecto.get("codigo") or lead_doc.get("codigo")
        if not property_code:
            return {}

        exec_name, exec_phone = find_responsible_executive(str(property_code))
        
        # Validar destino real
        if not exec_phone or exec_phone == "+56900000000" or exec_name == UNASSIGNED_LABEL:
            return {}

        logger.info(f"[PROCESS_SERVICE] Re-asignado lead {lead_doc.get('phone')} a {exec_name}")
        
        from .lead_router import get_next_business_slot
        now_cl = datetime.now(CHILE_TZ)
        assigned_at = get_next_business_slot(now_cl)
        
        return {
            "ejecutivo_asignado": exec_name,
            "prospecto.ejecutivo": exec_name,
            "lifecycle.assigned_at": assigned_at.isoformat(),
            "auto_reassigned": True
        }

    @staticmethod
    def process_lead(lead_id: Any, force: bool = False, force_notif: bool = False) -> bool:
        """
        Entry point único para procesar un lead (Clasificación + Asignación).
        """
        db = LeadProcessingService._db()
        try:
            query_id = ObjectId(lead_id) if isinstance(lead_id, str) else lead_id
            lead = db["leads"].find_one({"_id": query_id})
            if not lead:
                return False

            # 1. Chequeo de idempotencia
            needs_classification = not lead.get("cluster_id") or not lead.get("zone")
            
            unassigned_labels = [UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", None, ""]
            current_exec = lead.get("ejecutivo_asignado") or lead.get("prospecto", {}).get("ejecutivo")
            needs_assignment = current_exec in unassigned_labels

            if not force and not (needs_classification or needs_assignment):
                return False

            # 2. Ejecutar Lógica
            update_data = {}
            
            # Clasificación
            if force or needs_classification:
                classification = LeadProcessingService.classify(lead)
                update_data.update(classification)
                update_data["auto_classified"] = True

            # Asignación
            if force or needs_assignment:
                assignment = LeadProcessingService.reassign_if_needed(lead)
                update_data.update(assignment)

            if update_data:
                now_cl = datetime.now(CHILE_TZ)
                now_str = now_cl.isoformat()
                update_data["last_processed_at"] = now_str
                # Visibilidad en UI (Solo si no existe, para no pisar el historial real)
                if not lead.get("ultima_actualizacion_bi"):
                    update_data["ultima_actualizacion_bi"] = now_str

                db["leads"].update_one({"_id": query_id}, {"$set": update_data})
                
                # 3. Notificar si fue re-asignado o pedido explícitamente
                if update_data.get("auto_reassigned") or force_notif:
                    full_lead = db["leads"].find_one({"_id": query_id})
                    
                    # CORRECCIÓN: Estructurar la notificación para que webhook la entienda
                    prospecto_data = full_lead.get("prospecto", {})
                    prop_code = prospecto_data.get("codigo") or full_lead.get("codigo")
                    exec_name = full_lead.get("ejecutivo_asignado") or prospecto_data.get("ejecutivo")
                    
                    from .lead_router import get_executive_phone
                    exec_phone = get_executive_phone(exec_name) if exec_name else "+56900000000"
                    
                    structured_alert = {
                        "phone": full_lead.get("phone"),
                        "property_code": prop_code,
                        "lead_type": "ReasignacionAutomatica",
                        "target_name": exec_name,
                        "target_phone": exec_phone,
                        "nombre": prospecto_data.get("nombre") or full_lead.get("nombre", "Cliente"),
                        "last_message": "Asignado automáticamente por el motor de distribución."
                    }
                    
                    save_pending_notification(structured_alert)
                    logger.info(f"[PROCESS_SERVICE] Notificacion pendiente estructurada guardada para {full_lead.get('phone')} (destinado a {exec_name})")

                return True
            
            # Caso especial: Si force=True pero no hubo cambios, al menos actualizar timestamp de BI para visibilidad
            if force:
                if not lead.get("ultima_actualizacion_bi"):
                    now_str = datetime.now(CHILE_TZ).isoformat()
                    db["leads"].update_one({"_id": query_id}, {"$set": {"ultima_actualizacion_bi": now_str}})
                return True
                
            return False

        except Exception as e:
            logger.error(f"[PROCESS_SERVICE] Error procesando lead {lead_id}: {e}", exc_info=True)
            return False
