from pymongo import MongoClient
from config import Config
from datetime import datetime
import pytz

def registro_masivo_chile():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    usuarios_col = db["usuarios"]

    # Definir zona horaria de Chile
    tz_chile = pytz.timezone('America/Santiago')
    ahora_chile = datetime.now(tz_chile)
    
    # Formato de texto para que sea fácil de leer en Mongo
    fecha_texto = ahora_chile.strftime("%Y-%m-%d %H:%M:%S")

    lista_usuarios = [
        {"email": "jpcaro@procasa.cl", "nombre": "Jorge Pablo Caro", "tel": "+56940904971", "rol": "supervisor"},
        {"email": "pgalleguillos@procasa.cl", "nombre": "Pablo Galleguillos", "tel": "+56983219804", "rol": "agente"},
        {"email": "p.galleguil@gmail.com", "nombre": "Pablo Galleguillos (Personal)", "tel": "+56983219804", "rol": "supervisor"},
        {"email": "rgalleg59@gmail.com", "nombre": "Ronald Galleguillos", "tel": "+56995295922", "rol": "supervisor"}
    ]

    for u in lista_usuarios:
        user_data = {
            "username": u["email"].lower(),
            "email": u["email"].lower(),
            "nombre": u["nombre"],
            "telefono": u["tel"],
            "rol": u["rol"],
            "is_active": True,
            "created_at_local": fecha_texto, # Texto fijo con hora de Chile
            "timestamp": ahora_chile        # Objeto fecha (Mongo lo pasará a UTC)
        }
        usuarios_col.update_one({"email": u["email"].lower()}, {"$set": user_data}, upsert=True)
        print(f"✅ Sincronizado: {u['email']} | Hora Chile: {fecha_texto}")

if __name__ == "__main__":
    registro_masivo_chile()