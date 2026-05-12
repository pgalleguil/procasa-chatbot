import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def list_collections():
    db = get_db()
    print("Collections in DB:")
    for coll in db.list_collection_names():
        count = db[coll].count_documents({})
        print(f"{coll}: {count} docs")

if __name__ == "__main__":
    list_collections()
