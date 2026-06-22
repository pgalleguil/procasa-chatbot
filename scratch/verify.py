from chatbot.storage import get_db
db = get_db()
lead = db.leads.find_one({'phone': '56920466434'})
if lead:
    print(f"Phone: {lead.get('phone')}")
    print(f"Assigned: {lead.get('ejecutivo_asignado')}")
    print(f"Temperature: {lead.get('lead_temperature')}")
    print(f"SLA Status: {lead.get('sla_status')}")
    print(f"Resultado Chat: {lead.get('bi_analytics_global', {}).get('RESULTADO_CHAT')}")
else:
    print("Lead not found.")
