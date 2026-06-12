import sys
import os
sys.path.append(r"C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")

import time
from api_captacion import get_captacion_list, get_captacion_detail
from chatbot.storage import get_db

db = get_db()
doc = db.yapo_propiedades.find_one()
if not doc:
    print("No docs")
    sys.exit()

obj_id = str(doc["_id"])

t0 = time.time()
try:
    get_captacion_detail(obj_id)
    print(f"get_captacion_detail took: {time.time() - t0:.4f}s")
except Exception as e:
    print(f"Error detail: {e}")

t0 = time.time()
try:
    get_captacion_list(limit=10)
    print(f"get_captacion_list took: {time.time() - t0:.4f}s")
except Exception as e:
    print(f"Error list: {e}")
