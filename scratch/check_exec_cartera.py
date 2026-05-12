import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_exec_names_cartera():
    db = get_db()
    execs = db["universo_cartera"].distinct("ejecutivo")
    print("Executives in universo_cartera:")
    for e in sorted([str(x) for x in execs if x]):
        count = db["universo_cartera"].count_documents({"ejecutivo": e})
        print(f"- {e}: {count} properties")

if __name__ == "__main__":
    check_exec_names_cartera()
