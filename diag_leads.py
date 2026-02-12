from chatbot.storage import get_db
import json

db = get_db()
leads_cursor = db['leads'].find({'prospecto.nombre': {'$regex': 'Ignacio|Daniela', '$options': 'i'}})

print("--- LEAD DIAGNOSTIC ---")
for l in leads_cursor:
    info = {
        "nombre": l.get("prospecto", {}).get("nombre"),
        "phone": l.get("phone"),
        "pipeline_stage": l.get("pipeline_stage"),
        "stage": l.get("stage"),
        "crm_estado": l.get("crm_estado"),
        "assigned_at": l.get("lifecycle", {}).get("assigned_at"),
        "created_at": str(l.get("created_at")),
        "sla_warning_sent": l.get("sla_warning_sent")
    }
    print(json.dumps(info, indent=2))
