import sys
import os
sys.path.append(r"C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
from chatbot.storage import get_db
db = get_db()
indexes = db.yapo_propiedades.index_information()
for name, info in indexes.items():
    print(f"Index: {name}, Keys: {info['key']}")
