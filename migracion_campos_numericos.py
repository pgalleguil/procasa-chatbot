# migracion_campos_numericos_segura.py
from pymongo import MongoClient
from config import Config

config = Config()
client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]
coleccion = db["universo_obelix"]

# SOLO los 3 campos que necesitas
campos = ["dormitorios", "banos", "estacionamientos"]

print("Iniciando migración SEGURA de dormitorios, baños y estacionamientos...\n")

for campo in campos:
    print(f"Procesando {campo}...", end=" ")

    # Pipeline que convierte solo si el string representa un número válido
    pipeline = [
        {
            "$set": {
                campo: {
                    "$cond": {
                        "if": {
                            "$and": [
                                { "$ne": [{ "$type": f"${campo}" }, "missing"] },
                                { "$ne": [{ "$trim": { "input": f"${campo}" }}, ""] },
                                { "$regexMatch": { "input": { "$trim": { "input": f"${campo}" }}, "regex": "^\\d+$" } }
                            ]
                        },
                        "then": { "$toInt": { "$trim": { "input": f"${campo}" } } },
                        "else": f"${campo}"  # deja tal cual si no es número válido
                    }
                }
            }
        }
    ]

    resultado = coleccion.update_many(
        { campo: { "$type": "string" } },  # solo strings
        pipeline
    )

    print(f"→ {resultado.modified_count} documentos convertidos a número")

print("\n¡MIGRACIÓN COMPLETA Y SEGURA!")
print("Solo se convirtieron valores que eran números válidos en string.")
print("Los vacíos '' o con texto quedan como string (no rompen más).")

"""
PS C:\Users\pgall\Desktop\Python> & C:/Users/pgall/AppData/Local/Programs/Python/Python313/python.exe c:/Users/pgall/Desktop/Python/ChatBot_v6_Grok/migracion_campos_numericos.py
Tipos actuales después de la migración:

dormitorios:
   → int        : 1626 documentos
   → string     : 294 documentos

banos:
   → int        : 1673 documentos
   → string     : 247 documentos

estacionamientos:
   → int        : 1814 documentos
   → string     : 106 documentos
PS C:\Users\pgall\Desktop\Python> 
"""