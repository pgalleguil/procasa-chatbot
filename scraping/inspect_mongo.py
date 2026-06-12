import os
import sys
import json
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from pymongo import MongoClient

client = MongoClient(Config.MONGO_URI)
db = client[Config.DB_NAME]
coll = db["yapo_propiedades"]

doc = coll.find_one({"_id": ObjectId("69a9e261e5a625e02ca3fc21")})

if not doc:
    print("Documento no encontrado!")
else:
    print("==== DOC ====")
    print("es_propietario_directo:", doc.get("es_propietario_directo"), doc.get("details", {}).get("es_propietario_directo"))
    print("classification_state:", doc.get("classification_state"), doc.get("details", {}).get("classification_state"))
    print("company_name:", doc.get("company_name"), doc.get("details", {}).get("company_name"))
    print("pre_reclassification_backup:", doc.get("pre_reclassification_backup"))

    import scraping_yapo_proxys
    print("\n==== RUNNING RECLASSIFY ====")
    from reclassify_batch import reconstruct_signals_from_doc, classify_seller_state, extract_old_classification

    signals = reconstruct_signals_from_doc(doc)
    old = extract_old_classification(doc)
    new = classify_seller_state(**signals)

    print("SIGNALS:", signals)
    print("OLD STATE:", old)
    print("NEW STATE:", new)
