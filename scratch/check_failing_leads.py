import sys
import os
sys.path.append(os.getcwd())
from pymongo import MongoClient
from config import Config
from bson import ObjectId

def check_leads():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    leads_col = db["leads"]
    
    target_ids = [
        "695873fc3bfdb6de3d842708",
        "695c09cb3bfdb6de3d8439b4",
        "695c29173bfdb6de3d843a99"
    ]
    
    for lid in target_ids:
        try:
            lead = leads_col.find_one({"_id": ObjectId(lid)})
        except:
            # Maybe it's not an ObjectId in the string ID field
            lead = leads_col.find_one({"id": lid})
        
        if lead:
            print(f"Lead ID: {lid}")
            print(f"  Phone: {lead.get('phone')}")
            print(f"  Name: {lead.get('nombre')}")
            print(f"  Property Code: {lead.get('property_code')}")
            print(f"  Comuna: {lead.get('comuna')}")
            print(f"  Source: {lead.get('source')}")
            print("-" * 20)
        else:
            # Try finding by string _id if it was stored as string
            lead = leads_col.find_one({"_id": lid})
            if lead:
                print(f"Lead ID (string): {lid}")
                print(f"  Phone: {lead.get('phone')}")
                print(f"  Name: {lead.get('nombre')}")
                print(f"  Property Code: {lead.get('property_code')}")
                print("-" * 20)
            else:
                print(f"Lead {lid} not found in DB.")

if __name__ == "__main__":
    check_leads()
