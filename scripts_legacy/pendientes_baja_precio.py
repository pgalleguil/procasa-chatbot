#!/usr/bin/env python3
import pandas as pd
from pymongo import MongoClient
from config import Config

# ==============================================================================
# CONFIGURACIÓN
# ==============================================================================
OFICINA_FILTRO = "INMOBILIARIA SUCRE SPA"

CAMPANAS_ANTERIORES = [
    "ajuste_precio_202512", "ajuste_precio_regiones_202512",
    "ajuste_precio_202512_REPASO", "ajuste_precio_regiones_202512_REPASO",
    "ajuste_precio_202601_TERCER", "ajuste_precio_regiones_202601_TERCER",
    "ajuste_precio_202602_v4", "ajuste_precio_regiones_202602_v4"
]

# ==============================================================================d
# CONEXIÓN Y PIPELINE
# ==============================================================================
client = MongoClient(Config.MONGO_URI)
db = client[Config.DB_NAME]
contactos = db.contactos  # Ajusta si el nombre exacto de la colección es diferente

pipeline = [
    { "$match": {
        "tipo": "propietario",
        "update_price.campana_nombre": {"$in": CAMPANAS_ANTERIORES},
        #"$or": [
        #    {"estado": {"$exists": False}},
        #    {"estado": "pendiente_llamada"}
        #]
    }},
    { "$lookup": {
        "from": "universo_obelix",
        "localField": "codigo",
        "foreignField": "codigo",
        "as": "info"
    }},
    { "$unwind": "$info" },
    { "$match": {
        "info.disponible": True,
        "info.oficina": OFICINA_FILTRO
        # Sin filtro de región → incluye Región Metropolitana (Sucre)
    }},
    { "$project": {
        "_id": 0,
        "codigo": "$codigo",
        "apellido_paterno_propietario": "$info.apellido_paterno_propietario",
        "apellido_materno_propietario": "$info.apellido_materno_propietario",
        "nombre_propietario": "$info.nombre_propietario",
        "fono_propietario": "$info.fono_propietario",
        "movil_propietario": "$info.movil_propietario",
        "email_propietario": "$email_propietario",        # Este campo está en contactos
        "oficina": "$info.oficina",
        "ejecutivo": "$info.ejecutivo",
        "tipo": "$info.tipo",
        "operacion": "$info.operacion",
        "region": "$info.region",
        "comuna": "$info.comuna",
        "ultima_campana": "$update_price.campana_nombre",
        "estado": {"$ifNull": ["$estado", "sin estado"]}
    }},
    { "$sort": {"ejecutivo": 1, "comuna": 1} }
]

# ==============================================================================
# EJECUCIÓN Y EXPORTACIÓN
# ==============================================================================
resultados = list(contactos.aggregate(pipeline))

print(f"Se encontraron {len(resultados)} propiedades que cumplen los filtros.")

if resultados:
    df = pd.DataFrame(resultados)

    # Nombre completo en formato chileno típico: APELLIDO PATERNO APELLIDO MATERNO, NOMBRES
    df['nombre_completo'] = df.apply(
        lambda row: f"{row['apellido_paterno_propietario'] or ''} {row['apellido_materno_propietario'] or ''}, {row['nombre_propietario'] or ''}"
                    .strip().strip(',').strip(),
        axis=1
    )

    # Orden de columnas como pediste + extras útiles
    columnas = [
        "codigo",
        "nombre_completo",
        "apellido_paterno_propietario",
        "apellido_materno_propietario",
        "nombre_propietario",
        "email_propietario",
        "fono_propietario",
        "movil_propietario",
        "ejecutivo",
        "oficina",
        "tipo",
        "operacion",
        "region",
        "comuna",
        "ultima_campana",
        "estado"
    ]
    df = df[columnas]

    archivo = "propiedades_sucre_no_respondidas.xlsx"
    df.to_excel(archivo, index=False)
    print(f"\nExportado correctamente a: {archivo}")
    print(f"Total de filas: {len(df)}")
else:
    print("No se encontraron propiedades que cumplan los criterios.")