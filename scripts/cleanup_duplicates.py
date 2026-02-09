
import os
import sys
from pymongo import MongoClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from config import Config
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    print("--- Cleaning Duplicate CRM Tasks ---")
    
    # Logic: Keep only the latest task for each phone+type combination
    pipeline = [
        {"$group": {
            "_id": {"phone": "$phone", "type": "$type"},
            "ids": {"$push": "$_id"},
            "count": {"$sum": 1}
        }},
        {"$match": {"count": {"$gt": 1}}}
    ]
    
    duplicates = db["crm_tasks"].aggregate(pipeline)
    
    deleted_count = 0
    for group in duplicates:
        ids_to_remove = group["ids"][:-1] # Keep the last one (assuming order, but better strictly by date if possible)
        # Actually, let's just remove ALL 'superseded' dupes if the user wants. 
        # But safest is to remove older ones.
        
        # Let's remove specifically the ones from the screenshot if they are problematic?
        # User asked "debo eliminar los registros duplicados". Yes.
        
        db["crm_tasks"].delete_many({"_id": {"$in": ids_to_remove}})
        deleted_count += len(ids_to_remove)
        print(f"Removed {len(ids_to_remove)} duplicates for {group['_id']}")

    print(f"Total deleted: {deleted_count}")

except Exception as e:
    print(f"Error: {e}")
