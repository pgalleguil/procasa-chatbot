from chatbot.storage import get_db

db = get_db()
result = db.visitas.update_one(
    {"visita_code": "VIS-2026-1D92"},
    {"$set": {"security.transaction_uuid": "7599f64b-e73b-44d9-9933-01582ad52824"}}
)

if result.modified_count > 0:
    print("Camila's transaction UUID updated successfully in MongoDB.")
else:
    print("Document was not modified (perhaps it already had the correct UUID or visita_code didn't match).")
