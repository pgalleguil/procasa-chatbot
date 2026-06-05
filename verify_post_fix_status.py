import os
import sys
import asyncio
import unicodedata
import re
from collections import Counter
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import Config
from motor.motor_asyncio import AsyncIOMotorClient

def normalize(text):
    if not text: return ""
    text = str(text).lower().strip()
    text = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', text)

async def main():
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]

    yesterday_start = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    query = {"details.fecha_scraping": {"$gte": yesterday_start}}
    records = await coll.find(query).to_list(length=None)

    if not records:
        query = {"fecha_captura": {"$gte": (datetime.now(timezone.utc) - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)}}
        records = await coll.find(query).to_list(length=None)

    total = len(records)
    if total == 0:
        msg = "No se encontraron registros de ayer ni hoy."
        print(msg)
        with open("verify_post_fix_report.txt", "w", encoding="utf-8") as f:
            f.write(msg)
        return

    print(f"Analizando {total} registros...")

    duenos = 0
    corredores = 0

    # Sec 2
    comp_eq_pub = 0

    # Sec 3
    profile_sin_otras = 0

    # Sec 4
    top_company = Counter()
    top_broker = Counter()
    top_pub = Counter()

    for idx, doc in enumerate(records, 1):
        if idx % 100 == 0 or idx == total:
            print(f"Procesando {idx}/{total} ({(idx/total)*100:.1f}%)", end="\r", flush=True)

        details = doc.get("details", {})
        es_dueno = details.get("es_propietario_directo", False)
        pub = details.get("publicador", "N/A")
        comp = details.get("company_name", "N/A")
        brok = details.get("broker_brand", "N/A")
        prof_id = details.get("seller_profile_id", "N/A")
        is_pro = details.get("seller_is_pro", False)

        if es_dueno:
            duenos += 1
        else:
            corredores += 1

        # Sec 2: company_name == publicador (normalizado)
        if comp != "N/A" and pub != "N/A":
            if normalize(comp) == normalize(pub):
                comp_eq_pub += 1

        # Sec 3: corredor clasificado con profile_id pero sin otras señales
        if not es_dueno:
            has_prof = prof_id not in ("N/A", None, "")
            has_pro = is_pro == True
            has_brok = brok not in ("N/A", None, "")
            has_comp = comp not in ("N/A", None, "")

            if has_prof and not has_pro and not has_brok and not has_comp:
                profile_sin_otras += 1

        # Sec 4
        if comp not in ("N/A", None, ""): top_company[comp] += 1
        if brok not in ("N/A", None, ""): top_broker[brok] += 1
        if pub not in ("N/A", None, ""): top_pub[pub] += 1

    print()

    pct_duenos = (duenos / total * 100) if total > 0 else 0
    pct_corredores = (corredores / total * 100) if total > 0 else 0
    pct_comp_pub = (comp_eq_pub / total * 100) if total > 0 else 0
    pct_profile_sin = (profile_sin_otras / corredores * 100) if corredores > 0 else 0

    out = []
    out.append("==================================================================")
    out.append("VALIDACIÓN POST-FIX — Estado de MongoDB Ayer+Hoy")
    out.append(f"Generado: {datetime.now(timezone.utc).isoformat()}")
    out.append("==================================================================")

    out.append("\n==================================================================")
    out.append("SECCIÓN 1: DISTRIBUCIÓN GENERAL")
    out.append("==================================================================")
    out.append(f"Total registros ayer+hoy: {total}")
    out.append(f"Dueños:      {duenos:>6}  ({pct_duenos:.1f}%)")
    out.append(f"Corredores:  {corredores:>6}  ({pct_corredores:.1f}%)")

    out.append("\n==================================================================")
    out.append("SECCIÓN 2: CONTAMINACIÓN company_name == publicador")
    out.append("==================================================================")
    out.append(f"Registros donde company_name == publicador (normalizado): {comp_eq_pub}")
    out.append(f"Porcentaje sobre total: {pct_comp_pub:.1f}%")

    out.append("\n==================================================================")
    out.append("SECCIÓN 3: CORREDORES CON SOLO seller_profile_id (sin otras señales)")
    out.append("==================================================================")
    out.append("Criterio: es_propietario_directo=False, seller_is_pro=False,")
    out.append("          broker_brand=N/A, company_name=N/A, seller_profile_id existe")
    out.append(f"Cantidad: {profile_sin_otras}")
    out.append(f"Porcentaje sobre corredores: {pct_profile_sin:.1f}%")

    out.append("\n==================================================================")
    out.append("SECCIÓN 4: TOP 20 DISTRIBUCIONES")
    out.append("==================================================================")
    out.append("\nTOP 20 COMPANY_NAME:")
    for name, count in top_company.most_common(20):
        out.append(f"  {name} -> {count}")

    out.append("\nTOP 20 BROKER_BRAND:")
    for name, count in top_broker.most_common(20):
        out.append(f"  {name} -> {count}")

    out.append("\nTOP 20 PUBLICADOR:")
    for name, count in top_pub.most_common(20):
        out.append(f"  {name} -> {count}")

    out.append("\n==================================================================")
    out.append("SECCIÓN 5: CONCLUSIÓN AUTOMÁTICA")
    out.append("==================================================================")

    # company_name contaminado?
    if pct_comp_pub > 20:
        out.append(f"[ALERTA] company_name SIGUE CONTAMINADO: {pct_comp_pub:.1f}% de registros tienen company_name == publicador.")
    else:
        out.append(f"[OK] company_name parece normalizado: solo {pct_comp_pub:.1f}% coincide con publicador.")

    # profile_id sigue siendo señal dominante?
    if pct_profile_sin > 5:
        out.append(f"[ALERTA] seller_profile_id aun clasifica {profile_sin_otras} corredores sin otras senales ({pct_profile_sin:.1f}%).")
        out.append("  Revisar si la nueva logica esta siendo aplicada en los registros previos al fix.")
    else:
        out.append(f"[OK] seller_profile_id ya NO es senal dominante: solo {profile_sin_otras} casos residuales ({pct_profile_sin:.1f}%).")

    # Distribución normal?
    if 2 <= pct_duenos <= 20:
        out.append(f"[OK] Distribucion parece normal: {pct_duenos:.1f}% duenos, {pct_corredores:.1f}% corredores.")
    elif pct_duenos < 2:
        out.append(f"[ALERTA] Muy pocos duenos ({pct_duenos:.1f}%). Posible sobreclasificacion de corredores residual.")
    else:
        out.append(f"[INFO] Alta proporcion de duenos ({pct_duenos:.1f}%). Revisar si la correccion fue excesiva.")

    report = "\n".join(out)
    print(report)
    with open("verify_post_fix_report.txt", "w", encoding="utf-8") as f:
        f.write(report)

    client.close()

if __name__ == "__main__":
    asyncio.run(main())
