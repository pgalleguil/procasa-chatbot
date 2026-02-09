
import os
import sys
from pymongo import MongoClient
from pprint import pprint

# Add the parent directory to sys.path to import config
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from config import Config
    
    print(f"Connecting to {Config.MONGO_URI}...")
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    collection_name = "universo_obelix"
    if collection_name not in db.list_collection_names():
        print(f"Collection '{collection_name}' not found. Available: {db.list_collection_names()}")
    else:
        doc = db[collection_name].find_one()
        print(f"--- Document from {collection_name} ---")
        pprint(doc)

except ImportError:
    print("Could not import Config. Make sure this script is run from the correct location.")
except Exception as e:
    print(f"Error: {e}")
