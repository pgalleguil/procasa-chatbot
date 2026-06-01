import sys
from chatbot.storage import get_db

db = get_db()
v = db.visitas.find_one({'visita_code': 'VIS-2026-1D92'})
if v:
    print("Security fields:")
    for k, val in v.get('security', {}).items():
        print(f"  {k}: {val}")
else:
    print("Not found")
