
import os
import sys
from pymongo import MongoClient
from pprint import pprint

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from config import Config
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    print("--- Executives in 'usuarios' collection ---")
    users = db["usuarios"].find({}, {"nombre": 1, "telefono": 1, "movil": 1, "username": 1, "rol": 1})
    for user in users:
        print(f"Nombre: {user.get('nombre')}")
        print(f"  Usuario: {user.get('username')}")
        print(f"  Rol: {user.get('rol')}")
        print(f"  Telefono: {user.get('telefono')}")
        print(f"  Movil: {user.get('movil')}")
        print("-" * 20)

except Exception as e:
    print(f"Error: {e}")
