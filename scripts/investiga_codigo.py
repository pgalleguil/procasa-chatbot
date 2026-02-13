import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatbot.storage import get_db

db = get_db()
prop = db["universo_obelix"].find_one({"codigo": {"$regex": "64342"}})
print(f"Propiedad 64342: {prop}")

# Also search for anything with quotes
import re
all_quoted = list(db["universo_obelix"].find({"codigo": re.compile(r".*'.*")}))
print(f"Total con comillas: {len(all_quoted)}")
if all_quoted:
    print(f"Primeros 5 con comillas: {[d['codigo'] for d in all_quoted[:5]]}")
