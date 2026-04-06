import sys
import os
sys.path.append(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from config import Config
from pymongo import MongoClient

def main():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    cartera = db["universo_cartera"]

    query_all = {
        "oficina": {"$regex": "PROCASA SUCRE", "$options": "i"},
        "region": {"$regex": "Arica", "$options": "i"}
    }
    
    docs = list(cartera.find(query_all))
    
    disp_true = [d for d in docs if d.get('disponible') == True]
    disp_false = [d for d in docs if d.get('disponible') == False]
    
    print(f"Total Arica SUCRE: {len(docs)}")
    print(f"Disponible True: {len(disp_true)}")
    print(f"Disponible False: {len(disp_false)}")
    print("Estados de todos:")
    from collections import Counter
    c = Counter([d.get('estado') for d in docs])
    for k, v in c.items():
        print(f"  {k}: {v}")
        
    c_status = Counter([d.get('status') for d in docs])
    print("Status de todos:")
    for k, v in c_status.items():
        print(f"  {k}: {v}")

if __name__ == '__main__':
    main()
