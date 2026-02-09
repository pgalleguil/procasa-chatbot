
import os
import sys
from pymongo import MongoClient
from pprint import pprint

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from config import Config
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    target_names = ["Mariela Arriagada", "Susana Ensignia", "Erika Garrido"]
    
    for name in target_names:
        print(f"Searching for {name} in universo_obelix...")
        doc = db["universo_obelix"].find_one({"ejecutivo": name}, {"movil_ejecutivo": 1, "fono_ejecutivo": 1, "email_ejecutivo": 1})
        if doc:
            pprint(doc)
        else:
            print("Not found.")
        print("-" * 20)

except Exception as e:
    print(f"Error: {e}")
