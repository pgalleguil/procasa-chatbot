from chatbot.processing_service import LeadProcessingService
from bson import ObjectId
import sys

lead_id = "69d3e51d2be7175857b77e09"

print(f"--- Reprocesando Lead {lead_id} ---")
try:
    success = LeadProcessingService.process_lead(ObjectId(lead_id), force=True)
    if success:
        print("[OK] Lead procesado exitosamente.")
        # Verificamos como quedó
        db = LeadProcessingService._db()
        lead = db["leads"].find_one({"_id": ObjectId(lead_id)})
        print(f"Ejecutivo Asignado: {lead.get('ejecutivo_asignado')}")
        print(f"Cluster ID: {lead.get('cluster_id')}")
        print(f"Zone: {lead.get('zone')}")
    else:
        print("[SKIP] El procesamiento no realizo cambios o fallo.")
except Exception as e:
    print(f"[ERROR] {e}")
