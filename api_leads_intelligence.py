from pymongo import MongoClient
from config import Config
from datetime import datetime
from collections import Counter


# --------------------------------------------------
# FECHA REAL DE CREACIÓN DEL LEAD
# --------------------------------------------------
def get_creation_date(doc):
    try:
        return doc["_id"].generation_time.replace(tzinfo=None)
    except Exception:
        messages = doc.get("messages", [])
        if messages:
            ts = messages[0].get("timestamp")
            if ts:
                try:
                    return datetime.fromisoformat(ts.replace("Z", ""))
                except Exception:
                    pass
        return datetime.now()


# --------------------------------------------------
# SCORE DE CALIDAD (0–10)
# --------------------------------------------------
def calculate_score(prospecto, intencion):
    score = 0

    if prospecto.get("email"):
        score += 2
    if prospecto.get("rut"):
        score += 2
    if prospecto.get("nombre"):
        score += 1

    if intencion == "escalado_urgente":
        score += 5
    elif intencion == "agendar_visita":
        score += 4
    elif intencion == "contacto_directo":
        score += 3
    elif intencion == "consultar_precio":
        score += 1

    return min(score, 10)


# --------------------------------------------------
# INTENCIÓN MÁS FUERTE DEL HISTORIAL
# --------------------------------------------------
def determine_strongest_intent(messages):
    prioridades = [
        "escalado_urgente",
        "agendar_visita",
        "contacto_directo",
        "consultar_precio"
    ]

    intenciones = {
        m.get("intencion")
        for m in messages
        if m.get("role") == "assistant" and m.get("intencion")
    }

    for p in prioridades:
        if p in intenciones:
            return p

    return "consulta_general"


# --------------------------------------------------
# REPORTE EJECUTIVO
# --------------------------------------------------
def get_leads_executive_report():
    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]

        documentos = list(
            db["conversaciones_whatsapp"]
            .find({})
            .sort("_id", -1)
            .limit(2000)
        )

        if not documentos:
            return {"kpis": {}, "charts": {}, "leads": []}

        temporal_diario = {}
        comunas = Counter()
        fuentes = Counter()

        total_leads = len(documentos)
        leads_calientes = 0
        leads_calientes_con_datos = 0
        tiempos_conversion = []

        leads_table = []

        for doc in documentos:
            p = doc.get("prospecto", {})
            messages = doc.get("messages", [])

            phone = doc.get("phone")
            nombre = p.get("nombre", "")
            email = p.get("email")
            rut = p.get("rut")

            comuna = p.get("comuna") or "Sin Comuna"
            origen = p.get("origen") or "Directo"

            comunas[comuna] += 1
            fuentes[origen] += 1

            fecha_obj = get_creation_date(doc)
            fecha_str = fecha_obj.strftime("%Y-%m-%d")

            if fecha_obj.year >= datetime.now().year - 1:
                temporal_diario[fecha_str] = temporal_diario.get(fecha_str, 0) + 1

            intencion = determine_strongest_intent(messages)
            is_hot = intencion in [
                "agendar_visita",
                "escalado_urgente",
                "contacto_directo"
            ]

            if is_hot:
                leads_calientes += 1

                if email and rut:
                    leads_calientes_con_datos += 1

                try:
                    start_msg = messages[0] if messages else None
                    end_msg = next(
                        (
                            m for m in messages
                            if m.get("intencion") in [
                                "agendar_visita",
                                "escalado_urgente",
                                "contacto_directo"
                            ]
                        ),
                        None
                    )

                    if start_msg and end_msg:
                        t1 = datetime.fromisoformat(start_msg["timestamp"].replace("Z", ""))
                        t2 = datetime.fromisoformat(end_msg["timestamp"].replace("Z", ""))
                        delta = (t2 - t1).total_seconds() / 60
                        if 0 < delta < 43200:
                            tiempos_conversion.append(delta)
                except Exception:
                    pass

            score = calculate_score(p, intencion)

            leads_table.append({
                "nombre": nombre,
                "phone": phone,
                "email": email,
                "rut": rut,
                "intencion": intencion,
                "score": score,
                "origen": origen,
                "fecha": fecha_str,
                "hot_lead": is_hot
            })

        avg_speed = (
            sum(tiempos_conversion) / len(tiempos_conversion)
            if tiempos_conversion else 0
        )

        pct_hot_leads = (
            leads_calientes / total_leads * 100
            if total_leads > 0 else 0
        )

        # ✅ KPIs CORRECTOS
        tasa_captura_datos_hot = (
            leads_calientes_con_datos / leads_calientes * 100
            if leads_calientes > 0 else 0
        )

        penetracion_datos_total = (
            leads_calientes_con_datos / total_leads * 100
            if total_leads > 0 else 0
        )

        return {
            "kpis": {
                "total_leads": total_leads,
                "leads_calientes": leads_calientes,
                "pct_leads_calientes": round(pct_hot_leads, 1),
                "tasa_captura_datos_hot": round(tasa_captura_datos_hot, 1),
                "penetracion_datos_total": round(penetracion_datos_total, 1),
                "avg_speed_minutes": round(avg_speed, 1)
            },
            "charts": {
                "temporal": {
                    "labels": sorted(temporal_diario.keys()),
                    "values": [temporal_diario[d] for d in sorted(temporal_diario)]
                },
                "fuentes": {
                    "labels": list(fuentes.keys()),
                    "values": list(fuentes.values())
                }
            },
            "leads": leads_table
        }

    except Exception as e:
        print("❌ Error en reporte intelligence:", e)
        return {"kpis": {}, "charts": {}, "leads": []}


# --------------------------------------------------
# CHAT DE LEAD ESPECÍFICO
# --------------------------------------------------
def get_specific_lead_chat(phone):
    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
        doc = db["conversaciones_whatsapp"].find_one({"phone": phone})

        if not doc:
            return None

        return {
            "phone": doc.get("phone"),
            "prospecto": doc.get("prospecto", {}),
            "messages": doc.get("messages", [])
        }

    except Exception as e:
        print(f"❌ Error obteniendo chat {phone}:", e)
        return None
