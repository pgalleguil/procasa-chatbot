import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatbot.storage import get_db

db = get_db()
print("--- LEAD 56997511895 ---")
lead = db["leads"].find_one({"phone": {"$regex": "56997511895"}})
if lead:
    print(f"Lead ID: {lead['_id']}")
    print(f"Ejecutivo Asignado: {lead.get('ejecutivo_asignado')}")
    print(f"Propiedad: {lead.get('property_code')}")
    # Check history of assignments
    events = list(db["crm_events"].find({"phone": lead["phone"].replace("+", "")}, sort=[("timestamp", 1)]))
    print(f"Eventos: {len(events)}")
    for e in events:
        if e["type"] in ["ASSIGNMENT", "lead_assigned", "alert_sent"]:
            print(f"  {e['timestamp']} - {e['type']} - {e.get('to')} - {e.get('data', {}).get('executive')}")

print("\n--- PROPIEDAD 64342 ---")
prop = db["universo_obelix"].find_one({"codigo": {"$regex": "64342"}})
if prop:
    print(f"ID: {prop['_id']}")
    print(f"Codigo (repr): {repr(prop.get('codigo'))}")
    print(f"Ejecutivo: {prop.get('ejecutivo')}")
    print(f"Region: {prop.get('region')}")
else:
    print("Propiedad 64342 no encontrada incluso con regex.")
