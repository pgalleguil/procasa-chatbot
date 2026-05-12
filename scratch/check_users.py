import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_active_users():
    db = get_db()
    users = list(db["usuarios"].find({}, {"nombre": 1, "is_active": 1, "oficina": 1}))
    print("Users in DB:")
    for u in users:
        print(f"- {u.get('nombre')}: is_active={u.get('is_active')}, oficina={u.get('oficina')}")

if __name__ == "__main__":
    check_active_users()
