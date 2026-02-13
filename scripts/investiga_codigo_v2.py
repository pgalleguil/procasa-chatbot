import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatbot.storage import get_db

db = get_db()
print("--- BUSQUEDA POR CODIGO 64342 ---")
# Probar diferentes variaciones
for test_code in ["64342", 64342, "'64342'", "'64342'"]:
    prop = db["universo_obelix"].find_one({"codigo": test_code})
    print(f"Codigo: {repr(test_code)} -> Encontrado: {prop['_id'] if prop else 'No'}")

print("\n--- BUSQUEDA POR REGEX EN CODIGO ---")
regex_tests = [".*'.*", r"^'.*'$", r"^\s*'.*'\s*$"]
for r in regex_tests:
    count = db["universo_obelix"].count_documents({"codigo": {"$regex": r}})
    print(f"Regex: {r} -> Count: {count}")
    if count > 0:
        sample = db["universo_obelix"].find_one({"codigo": {"$regex": r}})
        print(f"   Ejemplo: {repr(sample['codigo'])}")
