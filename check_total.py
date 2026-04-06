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
    print(f"Total sin filtro de disponible: {len(docs)}")

if __name__ == '__main__':
    main()
