from pymongo import MongoClient
from config import Config
client = MongoClient(Config.MONGO_URI)
db = client[Config.DB_NAME]
doc = db['universo_obelix'].find_one()
print("Keys in universo_obelix:", list(doc.keys()) if doc else "None")
doc_cart = db['universo_cartera'].find_one()
print("Keys in universo_cartera:", list(doc_cart.keys()) if doc_cart else "None")
