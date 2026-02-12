from chatbot.storage import get_db
import json

db = get_db()
users_cursor = db['usuarios'].find({}).limit(10)

print("--- USERS DIAGNOSTIC ---")
for u in users_cursor:
    info = {
        "nombre": u.get("nombre"),
        "telefono": u.get("telefono"),
        "rol": u.get("rol"),
        "username": u.get("username"),
        "email": u.get("email")
    }
    print(json.dumps(info, indent=2))
