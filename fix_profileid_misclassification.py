import os
import sys
import asyncio
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from motor.motor_asyncio import AsyncIOMotorClient

# Importar la versión corregida de is_likely_broker desde el scraper
from scraping.scraping_yapo_proxys import is_likely_broker

async def main():
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]

    # Solo ayer y hoy
    yesterday_start = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    query = {
        "details.fecha_scraping": {"$gte": yesterday_start},
        "details.es_propietario_directo": False,
        "details.seller_is_pro": True,
        "$or": [
            {"details.broker_brand": {"$in": ["N/A", None, ""]}},
            {"details.broker_brand": {"$exists": False}}
        ],
        "$and": [
            {"$or": [
                {"details.company_name": {"$in": ["N/A", None, ""]}},
                {"details.company_name": {"$exists": False}}
            ]}
        ]
    }

    records = await coll.find(query).to_list(length=None)
    total_revisados = len(records)

    if total_revisados == 0:
        msg = "No se encontraron registros candidatos para reclasificación."
        print(msg)
        with open("fix_profileid_misclassification_report.txt", "w", encoding="utf-8") as f:
            f.write(msg)
        return

    print(f"Registros candidatos encontrados: {total_revisados}")
    print("Reevaluando con is_likely_broker() corregido...")

    reclasificados = 0
    permanecen_corredor = 0

    for idx, doc in enumerate(records, 1):
        if idx % 50 == 0 or idx == total_revisados:
            print(f"Procesando {idx}/{total_revisados} ({(idx/total_revisados)*100:.1f}%)", end="\r", flush=True)

        details = doc.get("details", {})
        publicador = details.get("publicador", "N/A")
        descripcion = details.get("descripcion_corta", details.get("descripcion", "N/A"))
        company_name = details.get("company_name", "N/A")
        seller_profile_id = details.get("seller_profile_id", "N/A")
        seller_is_pro = details.get("seller_is_pro", False)

        # Reevaluar con la función corregida (ya NO suma puntos por profile_id ni seller_is_pro)
        sigue_siendo_corredor = is_likely_broker(
            publicador,
            descripcion,
            company_name,
            seller_profile_id,
            seller_is_pro
        )

        if not sigue_siendo_corredor:
            # Actualizar en MongoDB
            await coll.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    "details.es_propietario_directo": True,
                    "details.confianza_propietario": 0.95,
                    "details.audit_fix": "profile_id_false_positive"
                }}
            )
            reclasificados += 1
        else:
            permanecen_corredor += 1

    print()  # Salto de línea

    pct_corregido = (reclasificados / total_revisados * 100) if total_revisados > 0 else 0

    out = []
    out.append("==================================================================")
    out.append("REPORTE DE CORRECCIÓN: profile_id false positives")
    out.append("==================================================================")
    out.append(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    out.append(f"\nRegistros revisados:          {total_revisados}")
    out.append(f"Registros reclasificados:     {reclasificados}")
    out.append(f"Permanecen como corredores:   {permanecen_corredor}")
    out.append(f"Porcentaje corregido:         {pct_corregido:.1f}%")
    out.append("\n==================================================================")
    out.append("CRITERIO APLICADO")
    out.append("==================================================================")
    out.append("Candidatos: es_propietario_directo=False AND seller_is_pro=True")
    out.append("            AND broker_brand=N/A AND company_name=N/A")
    out.append("\nReclasificación: is_likely_broker() corregido devolvió False")
    out.append("Campos actualizados:")
    out.append("  details.es_propietario_directo = True")
    out.append("  details.confianza_propietario  = 0.95")
    out.append("  details.audit_fix              = 'profile_id_false_positive'")

    report = "\n".join(out)
    print(report)
    with open("fix_profileid_misclassification_report.txt", "w", encoding="utf-8") as f:
        f.write(report)

    client.close()

if __name__ == "__main__":
    asyncio.run(main())
