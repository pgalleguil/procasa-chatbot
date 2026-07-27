import os, sys
import json
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient

BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "URLS")

if not MONGO_URI:
    print("ERROR: MONGO_URI no configurado en .env")
    sys.exit(1)

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
coll = db["contracts"]

total = coll.count_documents({})

results = {
    "total_contracts": total,
    "missing_property_data_field": 0,       # property_data no existe
    "property_data_null": 0,                 # property_data = null / None
    "property_data_not_dict": 0,             # property_data existe pero no es dict
    "missing_direccion": 0,                  # property_data.direccion vacio/ausente
    "missing_rol": 0,                        # property_data.rol vacio/ausente
    "have_property_code": 0,                 # property_code presente en otro campo top-level
    "missing_property_data_contracts": [],   # codigos de contratos sin property_data
    "missing_direccion_contracts": [],       # codigos sin direccion
    "complete_contracts": 0,
    "incomplete_contracts": 0,
}

# 1. property_data field missing entirely (explicit $exists: false)
missing_field_count = coll.count_documents({"property_data": {"$exists": False}})
results["missing_property_data_field"] = missing_field_count

# 2. property_data is null / None
null_count = coll.count_documents({"property_data": None})
results["property_data_null"] = null_count

# 3. property_data not a dict (could be array, string, etc.)
not_dict_count = coll.count_documents({
    "$and": [
        {"property_data": {"$exists": True}},
        {"property_data": {"$ne": None}},
        {"property_data": {"$not": {"$type": "object"}}}
    ]
})
results["property_data_not_dict"] = not_dict_count

# 4. property_data.direccion missing/empty
missing_dir_count = coll.count_documents({
    "$or": [
        {"property_data": {"$exists": False}},
        {"property_data": None},
        {"property_data.direccion": {"$exists": False}},
        {"property_data.direccion": None},
        {"property_data.direccion": ""},
    ]
})
results["missing_direccion"] = missing_dir_count

# 5. property_data.rol missing/empty
missing_rol_count = coll.count_documents({
    "$or": [
        {"property_data": {"$exists": False}},
        {"property_data": None},
        {"property_data.rol": {"$exists": False}},
        {"property_data.rol": None},
        {"property_data.rol": ""},
    ]
})
results["missing_rol"] = missing_rol_count

# 6. Check how many have property_code available elsewhere
have_prop_code = coll.count_documents({
    "$or": [
        {"property_data": {"$exists": False}},
        {"property_data": None},
    ],
    "property_code": {"$exists": True, "$ne": None, "$ne": ""}
})
results["have_property_code"] = have_prop_code

# 7. Fetch specific problematic contracts for detailed report
problematic = list(coll.find(
    {"$or": [
        {"property_data": {"$exists": False}},
        {"property_data": None},
    ]},
    {
        "contract_code": 1,
        "_id": 0,
        "status": 1,
        "property_code": 1,
        "property_data": 1,
        "propiedad_direccion": 1,
        "direccion": 1,
        "comuna": 1,
        "rol": 1,
        "cliente_nombre": 1,
        "created_at": 1,
        "created_by": 1,
        "executive": 1,
    }
).limit(50))

for doc in problematic:
    entry = {
        "contract_code": doc.get("contract_code", "N/A"),
        "status": doc.get("status", "N/A"),
        "property_code": doc.get("property_code", ""),
        "has_property_data": "property_data" in doc and doc["property_data"] is not None,
        "has_direccion_top": bool(doc.get("propiedad_direccion") or doc.get("direccion")),
        "has_comuna": bool(doc.get("comuna")),
        "has_rol": bool(doc.get("rol")),
        "has_cliente_nombre": bool(doc.get("cliente_nombre")),
        "created_by": doc.get("created_by", doc.get("executive", "?")),
    }
    results["missing_property_data_contracts"].append(entry)

# Also count contracts with missing direccion regardless of property_data
missing_dir_docs = list(coll.find(
    {"$or": [
        {"property_data": {"$exists": False}},
        {"property_data": None},
        {"property_data.direccion": {"$exists": False}},
        {"property_data.direccion": None},
        {"property_data.direccion": ""},
    ]},
    {"contract_code": 1, "_id": 0, "property_data": 1, "propiedad_direccion": 1}
).limit(50))

results["missing_direccion_contracts"] = [
    {
        "contract_code": d.get("contract_code", "N/A"),
        "has_top_level_dir": bool(d.get("propiedad_direccion")),
        "property_data": d.get("property_data"),
    }
    for d in missing_dir_docs
]

# Complete vs incomplete
complete_count = coll.count_documents({
    "property_data": {"$exists": True, "$ne": None, "$type": "object"},
    "property_data.direccion": {"$exists": True, "$ne": None, "$ne": ""},
    "property_data.rol": {"$exists": True, "$ne": None, "$ne": ""},
    "client_data": {"$exists": True, "$ne": None, "$type": "object"},
    "client_data.nombre": {"$exists": True, "$ne": None, "$ne": ""},
})
results["complete_contracts"] = complete_count
results["incomplete_contracts"] = total - complete_count

output_path = BASE_DIR / "audit_property_data.json"
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2, default=str)

print("=" * 70)
print("AUDITORIA DE property_data EN CONTRACTS")
print("=" * 70)
print(f"Total de contratos:                    {total}")
print(f"property_data ausente (campo no existe): {missing_field_count}")
print(f"property_data = null:                  {null_count}")
print(f"property_data no es dict:              {not_dict_count}")
print(f"Sin direccion en property_data:        {missing_dir_count}")
print(f"Sin rol en property_data:             {missing_rol_count}")
print(f"property_code alternativo disponible:  {have_prop_code}")
print(f"Contratos completos:                   {complete_count}")
print(f"Contratos incompletos:                {results['incomplete_contracts']}")
print("=" * 70)
print(f"\nContratos sin property_data (primeros {len(problematic)}):")
for p in results["missing_property_data_contracts"]:
    print(f"  {p['contract_code']}: status={p['status']} code={p['property_code']} "
          f"has_direccion_top={p['has_direccion_top']} has_rol={p['has_rol']} "
          f"by={p['created_by']}")
print(f"\nReporte detallado: {output_path}")

client.close()
