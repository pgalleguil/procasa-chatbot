import os
import sys

# Add parent directory to path
sys.path.append(os.getcwd())

from pymongo import MongoClient
from config import Config

def check_unique_execs():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    execs_1 = db["leads"].distinct("ejecutivo_asignado")
    execs_2 = db["leads"].distinct("prospecto.ejecutivo")
    execs_3 = db["universo_obelix"].distinct("ejecutivo")
    
    print(f"Unique execs (leads.ejecutivo_asignado): {execs_1}")
    print(f"Unique execs (leads.prospecto.ejecutivo): {execs_2}")
    print(f"Unique execs (universo_obelix.ejecutivo): {execs_3}")

if __name__ == "__main__":
    check_unique_execs()
