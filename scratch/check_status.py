import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_active_status():
    db = get_db()
    vals = db["universo_cartera"].distinct("disponible")
    print(f"Values for 'disponible': {vals}")
    
    # Check if there is an 'estado' or similar
    vals_status = db["universo_cartera"].distinct("status")
    print(f"Values for 'status': {vals_status}")

if __name__ == "__main__":
    check_active_status()
