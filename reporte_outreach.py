#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# reporte_outreach_v4: agrega métricas de búsqueda, rechazos, interacciones

import sys
import os
import argparse
import json
from datetime import datetime, timezone

# Path de proyecto
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from config import Config
from db import client, db

config = Config()

ORDENES_COLLECTION = db["ordenes_visitas"]
CHATS_COLLECTION = db["chats"]


def get_metrics(start_date_str: str = "2025-11-11"):
    """Calcula métricas generales y de comportamiento en chats."""
    start_date = datetime.fromisoformat(f"{start_date_str}T00:00:00+00:00").replace(tzinfo=timezone.utc)
    end_date = start_date.replace(hour=23, minute=59, second=59, microsecond=999999)

    # === OUTREACH ===
    total_leads = ORDENES_COLLECTION.count_documents({
        "outreach_enviado": {"$exists": True},
        "outreach_enviado.fecha": {"$gte": start_date, "$lt": end_date}
    })

    envios_exitosos = ORDENES_COLLECTION.count_documents({
        "outreach_enviado.fecha": {"$gte": start_date, "$lt": end_date},
        "outreach_enviado.exito": True
    })
    envios_fallidos = ORDENES_COLLECTION.count_documents({
        "outreach_enviado.fecha": {"$gte": start_date, "$lt": end_date},
        "$or": [
            {"outreach_enviado.exito": False},
            {"outreach_enviado.exito": {"$exists": False}}
        ]
    })
    tasa_exito = round((envios_exitosos / (envios_exitosos + envios_fallidos) * 100), 2) if (envios_exitosos + envios_fallidos) > 0 else 0

    # === CHATS ===
    chats_del_dia = list(CHATS_COLLECTION.find({
        "messages.timestamp": {"$gte": start_date, "$lt": end_date}
    }))

    total_chats = len(chats_del_dia)
    total_respuestas = 0
    total_busquedas = 0
    total_rechazos = 0
    total_no_contactar = 0
    total_leads_calientes = 0
    total_interacciones = 0

    for chat in chats_del_dia:
        msgs = chat.get("messages", [])
        total_interacciones += len(msgs)

        # Mensajes de usuario
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        if user_msgs:
            total_respuestas += 1  # cuenta como chat con interacción

        # Búsquedas reales
        if any(m.get("intent") == "BUSCAR_MAS" for m in user_msgs):
            total_busquedas += 1

        # Escalados
        if any(m.get("criteria", {}).get("escalar_humano") for m in msgs):
            total_leads_calientes += 1

        # Rechazos explícitos
        if any(m.get("intent") == "RECHAZO" for m in user_msgs):
            total_rechazos += 1

        # No contactar
        if chat.get("do_not_contact") or chat.get("current_criteria", {}).get("do_not_contact"):
            total_no_contactar += 1
        elif any("no te contactaremos más" in (m.get("content", "").lower()) for m in msgs if m.get("role") == "assistant"):
            total_no_contactar += 1

    tasa_respuesta = round((total_respuestas / total_leads * 100), 2) if total_leads > 0 else 0
    tasa_busqueda = round((total_busquedas / total_chats * 100), 2) if total_chats > 0 else 0
    tasa_no_contactar = round((total_no_contactar / total_chats * 100), 2) if total_chats > 0 else 0

    # === READ STATUS ===
    read_total = ORDENES_COLLECTION.count_documents({
        "outreach_enviado.fecha": {"$gte": start_date, "$lt": end_date},
        "mensaje_status": "read"
    })
    responding_phones = list(CHATS_COLLECTION.distinct(
        "phone",
        {"messages": {"$elemMatch": {"role": "user", "timestamp": {"$gte": start_date, "$lt": end_date}}}}
    ))
    read_no_reply = ORDENES_COLLECTION.count_documents({
        "outreach_enviado.fecha": {"$gte": start_date, "$lt": end_date},
        "mensaje_status": "read",
        "telefono": {"$nin": responding_phones}
    })

    tasa_read = round((read_total / total_leads * 100), 2) if total_leads > 0 else 0
    tasa_read_no_reply = round((read_no_reply / total_leads * 100), 2) if total_leads > 0 else 0

    promedio_interacciones = round(total_interacciones / total_chats, 1) if total_chats > 0 else 0

    metrics = {
        "fecha_informe": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "periodo": f"Primer día de outreach ({start_date_str})",
        "total_leads": total_leads,
        "envios_exitosos": envios_exitosos,
        "envios_fallidos": envios_fallidos,
        "tasa_exito_envio": tasa_exito,
        "chats_totales": total_chats,
        "total_respuestas": total_respuestas,
        "tasa_respuesta": tasa_respuesta,
        "total_busquedas": total_busquedas,
        "tasa_busqueda": tasa_busqueda,
        "total_rechazos": total_rechazos,
        "total_no_contactar": total_no_contactar,
        "tasa_no_contactar": tasa_no_contactar,
        "leads_calientes": total_leads_calientes,
        "promedio_interacciones_por_chat": promedio_interacciones,
        "read_total": read_total,
        "read_no_reply": read_no_reply,
        "tasa_read": tasa_read,
        "tasa_read_no_reply": tasa_read_no_reply,
    }

    return metrics


def print_informe(metrics: dict):
    print("\n=== INFORME OUTREACH - PROCASA ===")
    print(f"Fecha: {metrics['fecha_informe']}")
    print(f"Período: {metrics['periodo']}\n")

    print("| Métrica | Valor |")
    print("|---------|--------|")
    print(f"| Leads contactados | {metrics['total_leads']} |")
    print(f"| Envíos exitosos | {metrics['envios_exitosos']} |")
    print(f"| Tasa éxito envío (%) | {metrics['tasa_exito_envio']} |")
    print(f"| Chats totales | {metrics['chats_totales']} |")
    print(f"| Chats con respuesta | {metrics['total_respuestas']} |")
    print(f"| Tasa respuesta (%) | {metrics['tasa_respuesta']} |")
    print(f"| Solicitudes de búsqueda | {metrics['total_busquedas']} |")
    print(f"| Tasa búsquedas (%) | {metrics['tasa_busqueda']} |")
    print(f"| Leads calientes (escalados) | {metrics['leads_calientes']} |")
    print(f"| Promedio interacciones/chat | {metrics['promedio_interacciones_por_chat']} |")
    print(f"| Rechazos explícitos | {metrics['total_rechazos']} |")
    print(f"| No contactar (opt-out) | {metrics['total_no_contactar']} |")
    print(f"| Tasa no contactar (%) | {metrics['tasa_no_contactar']} |")
    print(f"| Leyeron el mensaje | {metrics['read_total']} |")
    print(f"| Leyeron pero no respondieron | {metrics['read_no_reply']} |")
    print(f"| Tasa leyeron (%) | {metrics['tasa_read']} |")
    print(f"| Warm leads (leyó y no respondió) (%) | {metrics['tasa_read_no_reply']} |")

    print("\n--- RESUMEN ---")
    print(
        f"Se contactaron {metrics['total_leads']} leads con una tasa de éxito de {metrics['tasa_exito_envio']}%. "
        f"Hubo {metrics['chats_totales']} chats activos, con {metrics['total_respuestas']} respuestas reales "
        f"({metrics['tasa_respuesta']}%), de los cuales {metrics['total_busquedas']} solicitaron búsquedas adicionales "
        f"({metrics['tasa_busqueda']}%). {metrics['leads_calientes']} fueron marcados como leads calientes.\n"
        f"Además, {metrics['total_no_contactar']} usuarios pidieron no ser contactados nuevamente "
        f"({metrics['tasa_no_contactar']}%). En promedio, hubo {metrics['promedio_interacciones_por_chat']} interacciones por chat.\n"
        f"De los mensajes enviados, {metrics['read_total']} fueron leídos ({metrics['tasa_read']}%), "
        f"y {metrics['read_no_reply']} no respondieron tras leer ({metrics['tasa_read_no_reply']}% warm leads)."
    )

    print(f"\nJSON Export:\n{json.dumps(metrics, indent=2, default=str)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Informe Outreach Procasa")
    parser.add_argument("--fecha", default="2025-11-11", help="Fecha del día (YYYY-MM-DD)")
    args = parser.parse_args()

    metrics = get_metrics(args.fecha)
    print_informe(metrics)
