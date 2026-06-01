import sys
from chatbot.storage import get_db

db = get_db()
print("Connected to DB successfully.")

# Find all visitas that were signed (security.token_used == True) but don't have status == 'signed'
query = {
    "security.token_used": True,
    "status": {"$ne": "signed"}
}

visitas = list(db.visitas.find(query))
print(f"Found {len(visitas)} signed visitas with incorrect status:")

for v in visitas:
    print(f"- Code: {v.get('visita_code')}, Client: {v.get('client_data', {}).get('nombre')}, Status: {v.get('status')}")
    # Update status to signed
    db.visitas.update_one(
        {"visita_code": v["visita_code"]},
        {"$set": {"status": "signed"}}
    )
    print(f"  -> Updated status to 'signed'")

print("Migration completed successfully.")
