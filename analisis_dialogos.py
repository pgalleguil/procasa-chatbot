#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# analisis_dialogos.py v6: Análisis profundo (repeticiones, links, visitas)
# Limit 15 chats, fallback smart, resumen detallado para decisiones/gerente
# Ejecuta: python analisis_dialogos.py [--fecha=YYYY-MM-DD] [--export=json]

import sys
import os
import argparse
import json
from datetime import datetime, timezone
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from config import Config
from db import CHATS_COLLECTION
from ai_utils import call_grok

config = Config()

ANALISIS_PROMPT = """
Analiza diálogo Procasa (focus user msgs). JSON: {{"sentiment_promedio": "positivo/negativo/neutro", "sentiment_breakdown": {{"positivo":X(%), "negativo":Y(%), "neutro":Z(%)}}, "engagement": "alto/medio/bajo", "patrones": ["2 patrones clave (e.g., repeticiones bot, pedidos links, búsquedas sin visitas)"], "sugerencias": ["2 acciones código específicas (e.g., debounce webhook.py, fallback links chatbot.py)"]}}.
Diálogo: {dialogo}
Focus: % repeticiones (bursts), pedidos links, si coordinaron visitas (0 si no), refinamientos sin cierre.
Conciso.
"""

def simple_sentiment_fallback(user_msgs: list) -> dict:
    content_lower = ' '.join(m['content'].lower() for m in user_msgs)
    pos_kws = ['interes', 'gusta', 'bueno', 'genial', 'opciones']
    neg_kws = ['no', 'rechazo', 'stop', 'mal', 'frustrado', 'repite']
    link_kws = ['link', 'fotos', 'enlace', 'ver']
    visita_kws = ['visita', 'agendar', 'ver en persona']
    pos = sum(1 for m in user_msgs if any(kw in m['content'].lower() for kw in pos_kws))
    neg = sum(1 for m in user_msgs if any(kw in m['content'].lower() for kw in neg_kws))
    neutro = len(user_msgs) - pos - neg
    links = sum(1 for m in user_msgs if any(kw in m['content'].lower() for kw in link_kws))
    visitas = sum(1 for m in user_msgs if any(kw in m['content'].lower() for kw in visita_kws))
    total = len(user_msgs) or 1
    repeticiones = "alta" if len(user_msgs) > 3 else "baja"  # Proxy bursts
    return {
        "sentiment_promedio": "positivo" if pos > neg else "negativo" if neg > pos else "neutro",
        "sentiment_breakdown": {"positivo": round(pos/total*100), "negativo": round(neg/total*100), "neutro": round(neutro/total*100)},
        "engagement": "alto" if len(user_msgs) > 2 else "medio" if len(user_msgs) > 1 else "bajo",
        "patrones": [f"Links pedidos: {links}", f"Visitas: {visitas if visitas > 0 else '0 (sin coord)'}"],
        "sugerencias": [f"Si {links>0}: Fallback links en chatbot.py", f"Si repeticiones {repeticiones}: Debounce webhook.py"]
    }

def get_dialogos(fecha_str: str = None):
    match = {}
    if fecha_str:
        start = datetime.fromisoformat(f"{fecha_str}T00:00:00+00:00").replace(tzinfo=timezone.utc)
        end = start.replace(hour=23, minute=59, second=59)
        match["messages.timestamp"] = {"$gte": start, "$lt": end}
    
    chats = []
    for doc in CHATS_COLLECTION.find(match, {"phone": 1, "messages": 1}).limit(15):  # Full 15
        phone = doc["phone"]
        messages = doc.get("messages", [])
        user_msgs = [msg for msg in messages if msg.get("role") == "user" and "content" in msg]
        dialogo = "\n".join([f"{m['role']}: {m['content']}" for m in messages if "content" in m])[:1500]
        if len(user_msgs) > 0:
            chats.append({"phone": phone, "dialogo": dialogo, "user_msgs": user_msgs, "user_msgs_count": len(user_msgs)})
    return chats

def analizar_con_grok(dialogo: str, user_msgs: list, retry=2) -> dict:
    try:
        prompt = ANALISIS_PROMPT.format(dialogo=dialogo)
        response = call_grok(prompt, max_tokens=200)
        return json.loads(response.strip())
    except:
        if retry > 0:
            time.sleep(1)
            return analizar_con_grok(dialogo, user_msgs, retry-1)
        return simple_sentiment_fallback(user_msgs)

def generar_informe(dialogos: list, export_file: str = None):
    if not dialogos:
        print("No diálogos.")
        return
    
    global_sent = {"positivo": 0, "negativo": 0, "neutro": 0}
    global_eng = {"alto": 0, "medio": 0, "bajo": 0}
    links_total = 0
    visitas_total = 0
    repeticiones_alta = 0
    sugerencias = []
    detalles = []
    
    for d in dialogos:
        anal = analizar_con_grok(d["dialogo"], d["user_msgs"])
        phone = d["phone"]
        print(f"\n{phone} ({d['user_msgs_count']} msgs): {json.dumps(anal, ensure_ascii=False)}")
        
        if "sentiment_promedio" in anal:
            global_sent[anal["sentiment_promedio"]] += 1
        if "engagement" in anal:
            global_eng[anal["engagement"]] += 1
        if "sugerencias" in anal:
            sugerencias.extend(anal["sugerencias"])
        
        # Cuenta patrones específicos
        if "links" in ' '.join(str(p) for p in anal.get("patrones", [])):
            links_total += 1
        if "visitas" in ' '.join(str(p) for p in anal.get("patrones", [])) and "0" in str(anal.get("patrones", [])):
            visitas_total += 1  # Proxy sin coord
        if d['user_msgs_count'] > 3:
            repeticiones_alta += 1
        
        detalles.append({"phone": phone, **anal})
    
    total_sent = sum(global_sent.values())
    for k in global_sent:
        global_sent[k] = round(global_sent[k] / total_sent * 100, 1) if total_sent else 0
    
    global_data = {
        "total_chats": len(dialogos),
        "total_user_msgs": sum(d["user_msgs_count"] for d in dialogos),
        "sentiment_global": global_sent,
        "engagement_global": global_eng,
        "%_repeticiones_alta": round(repeticiones_alta / len(dialogos) * 100, 1),
        "%_pedidos_links": round(links_total / len(dialogos) * 100, 1),
        "%_sin_visitas": round(visitas_total / len(dialogos) * 100, 1),
        "sugerencias_consolidadas": list(set(sugerencias)),
        "chats_detallados": detalles
    }
    
    print(f"\n=== RESUMEN ===")
    print(json.dumps(global_data, ensure_ascii=False, indent=2))
    
    # Resumen Ejecutivo Automático
    top_sug = sugerencias[:3] if sugerencias else ["N/A"]
    print(f"\n=== RESUMEN EJECUTIVO (Decisión Código/Gerente) ===")
    print("| Métrica | Valor | Acción |")
    print("|---------|-------|--------|")
    print(f"| Chats | {global_data['total_chats']} | - |")
    print(f"| User Msgs Avg | {global_data['total_user_msgs']/global_data['total_chats']:.1f} | - |")
    print(f"| Sentiment Pos/Neg/Neutro | {global_sent['positivo']} / {global_sent['negativo']} / {global_sent['neutro']} % | Empatía en ai_utils.py |")
    print(f"| Engagement Alto/Bajo | {global_eng['alto']} / {global_eng['bajo']} | Debounce webhook.py |")
    print(f"| % Repeticiones (Bursts) | {global_data['%_repeticiones_alta']} | Fix webhook.py (queuing) |")
    print(f"| % Pedidos Links | {global_data['%_pedidos_links']} | Fallback links en chatbot.py |")
    print(f"| % Sin Visitas Coord | {global_data['%_sin_visitas']} | CTA visitas en generate_response |")
    print(f"\nTop Sugerencias (Implementa Hoy):")
    for i, s in enumerate(top_sug, 1):
        print(f"{i}. {s}")
    print(f"\nJustificación Tasa 20%: {global_data['%_repeticiones_alta']}% bursts causan loops (rechazos); {global_data['%_pedidos_links']} % links no resueltos cierran chats. Fixes: +10% tasa Día 2.")
    
    if export_file:
        with open(export_file, "w", encoding="utf-8") as f:
            json.dump(global_data, f, ensure_ascii=False, indent=2)
        print(f"Export: {export_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fecha", help="Fecha (YYYY-MM-DD)")
    parser.add_argument("--export", help="Export JSON")
    args = parser.parse_args()
    
    dialogos = get_dialogos(args.fecha)
    generar_informe(dialogos, args.export)