import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatbot.storage import get_db

db = get_db()
print("--- BUSQUEDA DE CODIGOS CON CARACTERES NO NUMERICOS ---")
# Buscamos códigos que contengan algo que no sea dígito
import re
non_digit = list(db["universo_obelix"].find({"codigo": re.compile(r"[^\d]")}))
print(f"Total con caracteres no numéricos: {len(non_digit)}")
if non_digit:
    print(f"Primeros 10 ejemplares:")
    for d in non_digit[:10]:
        print(f"  ID: {d['_id']} | Codigo (repr): {repr(d.get('codigo'))}")

# Específicamente para 64342
print("\n--- BUSQUEDA EXACTA 64342 ---")
all_64342 = list(db["universo_obelix"].find({"codigo": {"$regex": "64342"}}))
for d in all_64342:
    print(f"ID: {d['_id']} | Codigo (repr): {repr(d.get('codigo'))}")
