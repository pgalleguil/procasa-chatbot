"""Reporte de leads por canal con timestamps clave.

Diferencia los canales de ingreso (Convecta/Prop360, WhatsApp, Lead Manual) y
expone, para cada lead, los cuatro hitos temporales relevantes:

  - fecha_entrada_canal : cuándo el dato entró en el canal de origen
                          (source_events.contact_date para Convecta)
  - fecha_scrapeo       : cuándo el scraper/ingesta lo capturó
                          (source_events.ingested_at para Convecta;
                           created_at para otros canales)
  - fecha_ingreso_db    : cuándo se creó el documento en `leads` (created_at)
  - fecha_asignacion    : cuándo se asignó al ejecutivo (lifecycle.assigned_at)

Uso:
  python scripts/report_leads_por_canal.py              # todo el historial
  python scripts/report_leads_por_canal.py --dias 7     # últimos 7 días
  python scripts/report_leads_por_canal.py --canal convecta
  python scripts/report_leads_por_canal.py --json       # salida JSON
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatbot.storage import get_db  # noqa: E402


def _norm(ts) -> str:
    if not ts:
        return ""
    return str(ts)


def _channel_of(lead: dict) -> str:
    """Clasifica el canal de ingreso de un lead."""
    canal_envio = str(lead.get("canal_envio") or "")
    source_type = str(lead.get("source_type") or "")
    origen = str(lead.get("origen") or "")

    if "Convecta" in canal_envio or source_type == "prop360":
        portal = canal_envio.replace("Convecta (Prop360)", "").strip(" -") or "Portal Inmobiliario"
        if portal in ("Proppit", "TocToc", "TOCTOC"):
            return f"Convecta ({portal})"
        return "Convecta"
    if canal_envio == "Lead Manual" or source_type == "manual":
        return f"Lead Manual ({origen or 'S/I'})"
    # Sin canal_envio ni source_type: evidencia de mensajería = WhatsApp
    msgs = lead.get("messages") or []
    has_user = any((m or {}).get("role") == "user" for m in msgs)
    if has_user or (lead.get("source_events")):
        return "WhatsApp"
    if origen:
        return f"Otro ({origen})"
    return "Sin clasificar"


def _timestamps(lead: dict) -> dict:
    se = lead.get("source_events") or []
    first = se[0] if se else {}
    lc = lead.get("lifecycle") or {}
    if first:
        return {
            "fecha_entrada_canal": _norm(first.get("contact_date")),
            "fecha_scrapeo": _norm(first.get("ingested_at")),
            "fecha_ingreso_db": _norm(lead.get("created_at")),
            "fecha_asignacion": _norm(lc.get("assigned_at")),
            "fuente": _norm(first.get("portal_source") or lead.get("origen")),
        }
    return {
        "fecha_entrada_canal": _norm(lead.get("last_message_at") or lead.get("created_at")),
        "fecha_scrapeo": _norm(lead.get("created_at")),
        "fecha_ingreso_db": _norm(lead.get("created_at")),
        "fecha_asignacion": _norm(lc.get("assigned_at")),
        "fuente": _norm(lead.get("origen")),
    }


def _parse_ts(s: str):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Reporte de leads por canal")
    ap.add_argument("--dias", type=int, default=None,
                    help="Solo leads ingresados en los últimos N días")
    ap.add_argument("--canal", type=str, default=None,
                    help="Filtrar por canal (convecta, whatsapp, manual)")
    ap.add_argument("--json", action="store_true", help="Salida JSON")
    args = ap.parse_args()

    db = get_db()
    now = datetime.now()
    cutoff = now - timedelta(days=args.dias) if args.dias else None

    rows = []
    for lead in db["leads"].find({}):
        created = _parse_ts(str(lead.get("created_at") or ""))
        if cutoff:
            comp = created.replace(tzinfo=None) if created else None
            cut = cutoff.replace(tzinfo=None)
            if comp and comp < cut:
                continue
            if not comp and args.dias:
                continue

        channel = _channel_of(lead)
        if args.canal:
            low = args.canal.lower()
            if low in ("convecta", "prop360") and "Convecta" not in channel:
                continue
            if low == "whatsapp" and channel != "WhatsApp":
                continue
            if low == "manual" and "Lead Manual" not in channel:
                continue
            if low not in ("convecta", "prop360", "whatsapp", "manual") and low not in channel.lower():
                continue

        row = {
            "phone": _norm(lead.get("phone")),
            "canal": channel,
            "ejecutivo": _norm(lead.get("ejecutivo_asignado")),
            "temperatura": _norm(lead.get("lead_temperature_effective")),
            "estado": _norm(lead.get("pipeline_stage")),
            **_timestamps(lead),
        }
        rows.append(row)

    rows.sort(key=lambda r: r["fecha_ingreso_db"], reverse=True)

    if args.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return

    # --- Consola ---
    counts = Counter(r["canal"] for r in rows)
    print("=" * 100)
    print("REPORTE DE LEADS POR CANAL")
    print(f"Periodo: {'todo el historial' if not args.dias else f'últimos {args.dias} días'}")
    print("=" * 100)
    print("\nResumen por canal:")
    for canal, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {n:5d}  {canal}")
    print(f"  {len(rows):5d}  TOTAL")

    print("\nDetalle (timestamps):")
    header = (f"{'PHONE':16} {'CANAL':26} {'ENTRADA CANAL':22} {'SCRAPEO':22} "
              f"{'INGRESO DB':22} {'ASIGNACION':22} {'EJECUTIVO':24}")
    print(header)
    print("-" * 100)
    for r in rows:
        print(f"{r['phone']:16} {r['canal']:26.26} {r['fecha_entrada_canal'][:19]:22} "
              f"{r['fecha_scrapeo'][:19]:22} {r['fecha_ingreso_db'][:19]:22} "
              f"{r['fecha_asignacion'][:19]:22} {r['ejecutivo'][:24]}")


if __name__ == "__main__":
    main()
