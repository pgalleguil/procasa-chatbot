from api_captacion import get_matching_leads_analysis
from chatbot.storage import get_db
from bson import ObjectId

db = get_db()
prop = db["yapo_propiedades"].find_one({"_id": ObjectId("69a8cd34e5a625e02ca3d369")})

if prop:
    analysis = get_matching_leads_analysis(prop)
    print(f"Prop: {prop.get('details', {}).get('comuna')}")
    print(f"Exact: {analysis['exact']}")
    print(f"Zone: {analysis['zone']}")
    print(f"Broad: {analysis['broad']}")
    print(f"Active this week (fixed): {analysis['active_recent']}")
else:
    print("Prop not found")
