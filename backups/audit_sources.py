import sys
sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from chatbot.storage import get_db
db = get_db()
pipeline = [
    {'$group': {'_id': {'$ifNull': ['$prospecto.origen', '__NULL__']}, 'count': {'$sum': 1}}},
    {'$sort': {'count': -1}}
]
results = list(db['leads'].aggregate(pipeline))
print("All source values in DB:")
for r in results:
    val = r['_id']
    if val == '__NULL__':
        val = None
    print(f"  {repr(val):40s} count={r['count']}")

# Check WhatsApp examples
print("\n--- WhatsApp samples (anonymized) ---")
whatsapp_leads = list(db['leads'].find(
    {'prospecto.origen': 'WhatsApp'},
    {'prospecto.nombre': 1, 'prospecto.origen': 1, 'phone': 1, 'prospecto.operacion': 1}
).limit(3))
for lead in whatsapp_leads:
    p = lead.get('prospecto', {}) or {}
    phone = str(lead.get('phone', ''))
    masked = phone[:4] + '****' + phone[-4:] if len(phone) >= 8 else '****'
    print(f"  nombre={p.get('nombre','?')} phone={masked} origen={p.get('origen','?')} operacion={p.get('operacion','?')}")

# Count unknown sources
null_values = [None, '', 'Sin informacion', 'Sin información', 'Desconocido', 'unknown', 'N/A', 'Sin informaci', '__NULL__']
unknown_count = 0
for r in results:
    if r['_id'] in null_values or (r['_id'] is None):
        unknown_count += r['count']
print(f"\nTotal unknown source leads: {unknown_count}")
