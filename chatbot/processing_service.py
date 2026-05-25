# chatbot/processing_service.py
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from .constants import CHILE_TZ, UNASSIGNED_LABEL
from .lead_router import find_responsible_executive
from .link_extractor import analizar_mensaje_para_link, extraer_codigo_internacional
from config import Config
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
        Calcula cluster_id y zone para un lead, buscando datos en universo_cartera si es necesario.
        """
        prospecto = lead_doc.get("prospecto", {}) or {}
        
        # 1. Obtener datos base
        comuna = prospecto.get("comuna") or lead_doc.get("comuna_interes")
        tipo = prospecto.get("tipo") or lead_doc.get("tipo_interes") or lead_doc.get("tipo_propiedad")
        operacion = prospecto.get("operacion") or lead_doc.get("operacion")
        
        # 2. Si falta comuna pero tenemos código, buscar en universo_cartera
        property_code = prospecto.get("codigo") or lead_doc.get("codigo")
        
        # [AUTOMATION] Si no hay código, intentar extraerlo de los mensajes (historial)
        if not property_code and lead_doc.get("messages"):
            logger.info(f"[PROCESS_SERVICE] Intentando RE-IDENTIFICAR lead {lead_doc.get('phone')} desde historial...")
            # Unimos los últimos mensajes para buscar links/códigos
            all_text = " ".join([m.get("content", "") for m in lead_doc.get("messages", [])[-5:]])
            found_link, prop_match, platform, code_raw = analizar_mensaje_para_link(all_text)
            
            if found_link and prop_match:
                property_code = str(prop_match.get("codigo"))
                logger.info(f"[PROCESS_SERVICE] ¡Match encontrado en historial! Código: {property_code}")
            else:
                # Probar código internacional
                c_int = extraer_codigo_internacional(all_text)
                if c_int:
                    db = LeadProcessingService._db()
                    prop_int = db["universo_cartera"].find_one({
                        "$or": [
                            {"codigo_internacional": c_int},
                            {"publicaciones.codigo_internacional": c_int}
                        ]
                    })
                    if prop_int:
                        property_code = str(prop_int.get("codigo"))
                        logger.info(f"[PROCESS_SERVICE] ¡Match por Cód Internacional en historial! Código: {property_code}")

        if not comuna and property_code:
            try:
                db = LeadProcessingService._db()
                p_code_int = int(property_code) if str(property_code).isdigit() else None
                prop = db["universo_cartera"].find_one({"codigo": p_code_int}) if p_code_int else None
                if not prop:
                    prop = db["universo_cartera"].find_one({"codigo": str(property_code)})
                
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
        comuna_norm = normalize_commune_v2(comuna)
        tipo_code = _normalize_tipo(tipo)
        op_code = _normalize_operacion(operacion)
        
        cluster_id = f"{comuna_ui}-{tipo_code}-{op_code}"
        zone = get_zone_for_comuna(comuna) if comuna else "unknown"
        
        return {
            "cluster_id": cluster_id,
            "zone": zone,
            "operacion": op_code,
            "tipo": tipo_code,
            "comuna": comuna,      # Root field for display
            "comuna_norm": comuna_norm # Added for optimized indexing
        }

    @staticmethod
    def calculate_score(lead_doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calcula el score de un lead basado en BI, comportamiento y origen.
        """
        bi_score = 0
        behavior_score = 0
        source_score = 0

        # A. BI Signals
        bi_res = lead_doc.get("bi_analytics_global", {}).get("RESULTADO_CHAT")
        if bi_res == "VISITA_SOLICITADA":
            bi_score += 50
        elif bi_res == "CONTACTO_HUMANO":
            bi_score += 40
        elif bi_res == "GIVE_OFFER":
            bi_score += 50

        # B. Behavioral Signals
        message_count = lead_doc.get("message_count", 0)
        messages = lead_doc.get("messages", [])
        if message_count > 1 or len(messages) > 1:
            behavior_score += 20
        
        last_activity = lead_doc.get("last_message_at") or lead_doc.get("last_updated") or lead_doc.get("updated_at")
        if last_activity:
            try:
                dt = datetime.fromisoformat(str(last_activity).replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = CHILE_TZ.localize(dt)
                else:
                    dt = dt.astimezone(CHILE_TZ)
                
                now = datetime.now(CHILE_TZ)
                if (now - dt).total_seconds() < 86400: # 24h
                    behavior_score += 20
            except:
                pass
        
        prospecto = lead_doc.get("prospecto", {})
        has_property_code = prospecto.get("codigo") or lead_doc.get("codigo") or lead_doc.get("property_code")
        if has_property_code or lead_doc.get("url"):
            behavior_score += 15
            
        origen = lead_doc.get("origen", "")
        stage = str(lead_doc.get("stage") or lead_doc.get("pipeline_stage") or "").upper()
        if any(x in origen for x in ["Yapo", "Portal", "Mercado"]) and stage == "NEW":
            behavior_score += 25

        # C. Source Signals
        if lead_doc.get("source_type") == "manual":
            source_score += 40

        total = bi_score + behavior_score + source_score
        return {
            "total": total,
            "breakdown": {
                "bi": bi_score,
                "behavior": behavior_score,
                "source": source_score
            }
        }

    @staticmethod
    def is_worthy_of_assignment(lead_doc: Dict[str, Any]) -> bool:
        """
        Determina si un lead merece ser asignado a un ejecutivo humano de forma PROACTIVA
        utilizando el sistema de scoring multi-capa.
        """
        score_data = LeadProcessingService.calculate_score(lead_doc)
        total_score = score_data["total"]
        threshold = getattr(Config, "LEAD_ASSIGNMENT_THRESHOLD", 40)
        
        # Guardamos el score_data temporalmente en el doc para poder loggearlo después
        lead_doc["_temp_score_data"] = score_data
        
        if total_score >= threshold:
            return True

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
            logger.debug(f"[PROCESS_SERVICE] Lead {lead_doc.get('phone')} descartado para auto-asignación (Baja intención/Histórico)")
            return {}

        property_code = prospecto.get("codigo") or lead_doc.get("codigo")
        comuna = prospecto.get("comuna") or lead_doc.get("comuna")
        zone = lead_doc.get("zone", "unknown")

        exec_name, exec_phone, assignment_type = find_responsible_executive(
            property_code=str(property_code) if property_code else None, 
            comuna=comuna, 
            zone=zone,
            lead_phone=lead_doc.get("phone"),
            lead_name=lead_doc.get("nombre") or lead_doc.get("prospecto", {}).get("nombre")
        )
        
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
            "auto_reassigned": True,
            "assignment_type": assignment_type
        }

    @staticmethod
    def process_lead(lead_id: Any, force: bool = False, force_notif: bool = False) -> bool:
        """
        Entry point único para procesar un lead (Clasificación + Asignación).
        """
        db = LeadProcessingService._db()
        try:
            query_id = ObjectId(lead_id) if isinstance(lead_id, str) else lead_id
            # Proyección acotada para reducir I/O de documentos grandes (messages puede crecer mucho).
            lead_projection = {
                "phone": 1,
                "nombre": 1,
                "codigo": 1,
                "created_at": 1,
                "timestamp": 1,
                "stage": 1,
                "pipeline_stage": 1,
                "cluster_id": 1,
                "zone": 1,
                "ejecutivo_asignado": 1,
                "origen": 1,
                "comuna": 1,
                "comuna_interes": 1,
                "tipo_interes": 1,
                "tipo_propiedad": 1,
                "operacion": 1,
                "message_count": 1,
                "last_message_at": 1,
                "last_updated": 1,
                "updated_at": 1,
                "url": 1,
                "ultima_actualizacion_bi": 1,
                "prospecto": 1,
                "messages": {"$slice": -5}
            }
            lead = db["leads"].find_one({"_id": query_id}, lead_projection)
            if not lead:
                return False

            # --- AUTO-ARCHIVADO DE LEADS ANTIGUOS (Solicitado por usuario) ---
            from datetime import timedelta
            now_cl = datetime.now(CHILE_TZ)
            created_at_val = lead.get("created_at") or lead.get("timestamp")
            if created_at_val and not force:
                try:
                    if isinstance(created_at_val, str):
                        created_dt = datetime.fromisoformat(created_at_val.replace('Z', '+00:00'))
                    else:
                        created_dt = created_at_val
                    
                    if created_dt.tzinfo is None:
                        created_dt = CHILE_TZ.localize(created_dt)
                    
                    if (now_cl - created_dt) > timedelta(days=90):
                        logger.debug(f"[PROCESS_SERVICE] Lead {lead_id} tiene más de 90 días. Archivando automáticamente.")
                        db["leads"].update_one(
                            {"_id": query_id}, 
                            {"$set": {"stage": "ARCHIVED", "archive_reason": "Lead antiguo (>90 días) sin procesar"}}
                        )
                        return True
                except Exception as e_arch:
                    logger.warning(f"[PROCESS_SERVICE] Error calculando antigüedad de lead {lead_id}: {e_arch}")

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
                update_data["last_processed_at"] = now_cl
                # Visibilidad en UI (Solo si no existe, para no pisar el historial real)
                if not lead.get("ultima_actualizacion_bi"):
                    update_data["ultima_actualizacion_bi"] = now_cl

                db["leads"].update_one({"_id": query_id}, {"$set": update_data})
                
                # 3. Notificar si fue re-asignado o pedido explícitamente
                if update_data.get("auto_reassigned") or force_notif:
                    # Evita un find_one adicional: armamos snapshot con los datos ya disponibles.
                    prospecto_data = lead.get("prospecto", {}) or {}
                    exec_name = update_data.get("ejecutivo_asignado") or update_data.get("prospecto.ejecutivo") or lead.get("ejecutivo_asignado") or prospecto_data.get("ejecutivo")
                    prop_code = prospecto_data.get("codigo") or lead.get("codigo")
                    
                    from .lead_router import get_executive_phone
                    exec_phone = get_executive_phone(exec_name) if exec_name else "+56900000000"
                    
                    structured_alert = {
                        "phone": lead.get("phone"),
                        "property_code": prop_code,
                        "lead_type": "ReasignacionAutomatica",
                        "target_name": exec_name,
                        "target_phone": exec_phone,
                        "nombre": prospecto_data.get("nombre") or lead.get("nombre", "Cliente"),
                        "last_message": "Asignado automáticamente por el motor de distribución."
                    }
                    
                    save_pending_notification(structured_alert)
                    logger.info(f"[PROCESS_SERVICE] Notificacion pendiente estructurada guardada para {lead.get('phone')} (destinado a {exec_name})")

                # --- NUEVO: STRUCTURED LOGGING PARA DECISIONES ---
                import json
                temp_score_data = lead.get("_temp_score_data", {"total": 0, "breakdown": {"bi": 0, "behavior": 0, "source": 0}})
                
                decision_log = {
                    "lead_id": str(query_id),
                    "score": temp_score_data.get("total", 0),
                    "bi_score": temp_score_data.get("breakdown", {}).get("bi", 0),
                    "behavior_score": temp_score_data.get("breakdown", {}).get("behavior", 0),
                    "source_score": temp_score_data.get("breakdown", {}).get("source", 0),
                    "threshold": getattr(Config, "LEAD_ASSIGNMENT_THRESHOLD", 40),
                    "decision": "ASSIGNED" if update_data.get("auto_reassigned") else "SKIPPED",
                    "assignment_type": update_data.get("assignment_type", "N/A"),
                    "reason": "fallback_regional" if update_data.get("assignment_type") in ["COMMUNE_FALLBACK", "ZONE_FALLBACK"] else "property_match" if update_data.get("assignment_type") == "PROPERTY" else "low_score_or_handled"
                }
                logger.debug(f"[PROCESS_SERVICE_DECISION] {json.dumps(decision_log)}")

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
