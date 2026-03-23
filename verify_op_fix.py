from api_captacion import get_matching_leads_analysis
from chatbot.storage import get_db
from bson import ObjectId

db = get_db()
p = db['yapo_propiedades'].find_one({'_id': ObjectId('69a8cd34e5a625e02ca3d369')})
if p:
    analysis = get_matching_leads_analysis(p)
    # The cluster_id contains the operation code at the end
    print(f"CLUSTER ID: {analysis.get('cluster_id')}")
    # Check threshold logic (inferred)
    print(f"OP CODE: {analysis['cluster_id'].split('-')[-1]}")
    # Active recent should be calculated using 30 days if it's 'A'
    print(f"ACTIVE RECENT: {analysis.get('active_recent')}")
else:
    print("Property not found")
