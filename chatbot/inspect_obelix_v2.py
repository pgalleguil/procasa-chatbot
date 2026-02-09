
import os
import sys
from pymongo import MongoClient
from pprint import pprint

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from config import Config
    
    print(f"Connecting to {Config.MONGO_URI}...")
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    collection_name = "universo_obelix"
    start_doc = db[collection_name].find_one()
    
    if start_doc:
        print("Keys found:", list(start_doc.keys()))
        # Print specific fields relevant to the task
        fields_to_check = ["codigo", "region", "comuna", "propietario", "ejecutivo", "Dueño", "Region", "Comuna", "Ejecutivo"]
        subset = {k: start_doc.get(k, "NOT FOUND") for k in fields_to_check}
        pprint(subset)
    else:
        print("No documents found in universe_obelix")

except Exception as e:
    print(f"Error: {e}")
