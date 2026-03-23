from chatbot.storage import get_db
from bson import ObjectId

db = get_db()
p = db['yapo_propiedades'].find_one({'_id': ObjectId('69a8cd34e5a625e02ca3d369')})
if p:
    details = p.get('details', {})
    print(f"OPERACION: '{details.get('operacion')}'")
    print(f"FULL DETAILS: {details}")
else:
    print("Property not found")
