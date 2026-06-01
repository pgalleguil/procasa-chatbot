import sys
from datetime import datetime
from chatbot.storage import get_db

db = get_db()
print("Connected to DB successfully.")

# Get recent visitas
visitas = list(db.visitas.find().sort("created_at", -1).limit(10))
print(f"Found {len(visitas)} recent visitas:")
for idx, v in enumerate(visitas):
    print(f"\n--- [{idx + 1}] Visita ---")
    print(f"Code: {v.get('visita_code')}")
    print(f"Created At: {v.get('created_at_local') or v.get('created_at')}")
    print(f"Status: {v.get('status')}")
    client = v.get('client_data', {})
    print(f"Client: {client.get('nombre')} | Phone: {v.get('phone')} | Email: {client.get('email')}")
    print(f"Property Code: {v.get('property_code')}")
    sec = v.get('security', {})
    print(f"Security Token: {sec.get('token')}")
    print(f"Security Token Expiry: {sec.get('token_expiry')}")
    print(f"Token Used: {sec.get('token_used')}")
    print(f"Signed PDF Path: {sec.get('signed_pdf_path')}")
    print(f"Timeline:")
    for t in v.get('timeline', []):
        print(f"  - {t.get('action')} at {t.get('server_timestamp') or t.get('timestamp')}")
