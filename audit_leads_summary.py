from pymongo import MongoClient
from datetime import datetime
from collections import Counter
from config import Config


HOT_INTENTS = {
    "agendar_visita",
    "escalado_urgente",
    "contacto_directo"
}

INTENT_PRIORITY = [
    "escalado_urgente",
    "agendar_visita",
    "contacto_directo",
    "consultar_precio",
    "consulta_general"
]


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", ""))


def strongest_intent(messages):
    found = set(
        m.get("intencion")
        for m in messages
        if m.get("role") == "assistant" and m.get("intencion")
    )

    for i in INTENT_PRIORITY:
        if i in found:
            return i

    return "consulta_general"


def first_hot_timestamp(messages):
    for m in messages:
        if m.get("intencion") in HOT_INTENTS:
            return parse_ts(m["timestamp"])
    return None


def get_creation_date(doc):
    if "_id" in doc:
        try:
            return doc["_id"].generation_time.replace(tzinfo=None)
        except Exception:
            pass

    if "created_at" in doc:
        return parse_ts(doc["created_at"])

    return None


def run_audit(limit=2000):
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]

    docs = list(
        db["conversaciones_whatsapp"]
        .find({})
        .sort("_id", -1)
        .limit(limit)
    )

    summary = {
        "total_leads": 0,
        "hot_leads": 0,
        "hot_leads_con_datos": 0,
        "speed_minutes": [],
        "intenciones": Counter(),
        "origenes": Counter()
    }

    rows = []

    for doc in docs:
        summary["total_leads"] += 1

        messages = doc.get("messages", [])
        prospecto = doc.get("prospecto", {})

        intent = strongest_intent(messages)
        is_hot = intent in HOT_INTENTS

        summary["intenciones"][intent] += 1
        summary["origenes"][prospecto.get("origen", "Desconocido")] += 1

        has_email = bool(prospecto.get("email"))
        has_rut = bool(prospecto.get("rut"))
        has_nombre = bool(prospecto.get("nombre"))

        if is_hot:
            summary["hot_leads"] += 1

            if has_email and has_rut:
                summary["hot_leads_con_datos"] += 1

            created = get_creation_date(doc)
            hot_ts = first_hot_timestamp(messages)

            if created and hot_ts:
                delta = (hot_ts - created).total_seconds() / 60
                if 0 < delta < 43200:
                    summary["speed_minutes"].append(delta)

        rows.append({
            "phone": doc.get("phone"),
            "intent": intent,
            "hot": is_hot,
            "email": has_email,
            "rut": has_rut,
            "nombre": has_nombre,
            "origen": prospecto.get("origen")
        })

    avg_speed = (
        sum(summary["speed_minutes"]) / len(summary["speed_minutes"])
        if summary["speed_minutes"] else 0
    )

    result = {
        "total_leads": summary["total_leads"],
        "hot_leads": summary["hot_leads"],
        "hot_leads_con_datos": summary["hot_leads_con_datos"],
        "tasa_captura_hot": round(
            (summary["hot_leads_con_datos"] / summary["hot_leads"] * 100)
            if summary["hot_leads"] else 0,
            1
        ),
        "avg_speed_minutes": round(avg_speed, 1),
        "intenciones": dict(summary["intenciones"]),
        "origenes": dict(summary["origenes"])
    }

    return result, rows


if __name__ == "__main__":
    resumen, filas = run_audit()

    print("\n===== RESUMEN AUDITORÍA =====")
    for k, v in resumen.items():
        print(f"{k}: {v}")
