import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def find_prop_by_code():
    db = get_db()
    
    code = "64053"
    # Search in all fields of all docs in yapo_propiedades
    # This is a bit slow but effective for finding where the code is
    print(f"Searching for code '{code}' in yapo_propiedades...")
    
    docs = list(db["yapo_propiedades"].find({}).limit(1000))
    for d in docs:
        if code in str(d):
            print(f"Found in document ID: {d['_id']}")
            # Find the path to the code
            def find_path(obj, target, current_path=""):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if find_path(v, target, f"{current_path}.{k}"): return True
                elif isinstance(obj, list):
                    for i, v in enumerate(obj):
                        if find_path(v, target, f"{current_path}[{i}]"): return True
                elif str(obj) == target:
                    print(f"Path: {current_path}")
                    return True
                return False
            find_path(d, code)
            break
    else:
        print("Not found in the first 1000 docs.")

if __name__ == "__main__":
    find_prop_by_code()
