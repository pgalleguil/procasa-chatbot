"""Reporte de propiedades problemáticas según las notas de gestión del CRM.

Los ejecutivos registran en el lead (sticky_notes / resultado de gestión)
cuando una propiedad ya no está disponible, el propietario no atiende, los
datos no existen, etc.  Este script agrupa por código de propiedad los leads
afectados y el motivo detectado, para saber cuáles suspender.

Uso:
  python scripts/report_propiedades_problema.py             # consola
  python scripts/report_propiedades_problema.py --json      # JSON
  python scripts/report_propiedades_problema.py --desde 2026-07-01
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatbot.storage import get_db  # noqa: E402


MOTIVO_PATTERNS = [
    ("PROPIEDAD_NO_DISPONIBLE", [
        r"no disponible", r"ya no se vende", r"no esta a la venta", r"no está a la venta",
        r"fuera de la venta", r"no se encuentra disponible",
    ]),
    ("VENDIDA", [
        r"\bvendid[oa]\b", r"vendido hace", r"vendida hace", r"\barrendad[oa]\b", r"arrendado",
    ]),
    ("PROPIETARIO_RETIRO", [
        r"propietario_retiro", r"retir[oa]d[oa]", r"propietari[oa].*retir", r"dueñ[oa].*retir",
    ]),
    ("PROPIETARIO_NO_ATIENDE", [
        r"no atiende", r"no responde", r"no contesta", r"fuera de servicio",
        r"no me puedo comunicar", r"no puedo comunicarme", r"no se puede contactar",
        r"telefono.*no.*existe", r"número telefónico mal ingresado", r"numero telefonico mal ingresado",
        r"telefono.*fuera de servicio", r"tel[eé]fono inv[áa]lido", r"numero invalido",
    ]),
    ("DATOS_NO_EXISTEN", [
        r"no existe", r"datos no existen", r"datos del due", r"no me arroja", r"no arroja",
        r"informaci[oó]n inexistente", r"ficha inexistente",
    ]),
    ("NO_AUTORIZA_GESTION", [
        r"no_autoriza_gestion", r"no autoriza", r"no autoriz[oa]",
    ]),
]


def _detect_motivo(text: str) -> str:
    if not text:
        return ""
    t = str(text).lower()
    for motivo, patterns in MOTIVO_PATTERNS:
        for p in patterns:
            if re.search(p, t):
                return motivo
    return ""


def _codigo_de(lead: dict) -> str:
    p = lead.get("prospecto") or {}
    return str(p.get("codigo") or lead.get("codigo") or lead.get("property_code") or "?")


def main() -> None:
    ap = argparse.ArgumentParser(description="Propiedades problemáticas por notas de gestión")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--desde", type=str, default=None, help="YYYY-MM-DD (fecha de la nota)")
    args = ap.parse_args()

    db = get_db()
    desde = None
    if args.desde:
        desde = datetime.fromisoformat(args.desde)

    # 1) sticky_notes del lead
    prop_motivos = defaultdict(Counter)     # codigo -> Counter(motivo)
    prop_notas = defaultdict(list)          # codigo -> lista de (lead_id, phone, ejecutivo, nota, fecha)
    prop_ejecutivos = defaultdict(set)
    prop_origenes = defaultdict(Counter)    # codigo -> Counter(origen)

    for lead in db["leads"].find({}, {
        "phone": 1, "prospecto": 1, "codigo": 1, "property_code": 1,
        "ejecutivo_asignado": 1, "sticky_notes": 1,
    }):
        codigo = _codigo_de(lead)
        origen = str((lead.get("prospecto") or {}).get("origen") or lead.get("origen") or "S/I")
        for n in (lead.get("sticky_notes") or []):
            content = str(n.get("content") or "")
            motivo = _detect_motivo(content)
            if not motivo:
                continue
            fecha = n.get("timestamp_iso") or n.get("created_at_str")
            if desde and fecha:
                try:
                    if datetime.fromisoformat(str(fecha)) < desde:
                        continue
                except ValueError:
                    pass
            prop_motivos[codigo][motivo] += 1
            prop_origenes[codigo][origen] += 1
            prop_notas[codigo].append({
                "lead_id": str(lead.get("_id")), "phone": lead.get("phone"),
                "ejecutivo": lead.get("ejecutivo_asignado"),
                "origen": origen,
                "nota": content, "fecha": fecha,
            })
            prop_ejecutivos[codigo].add(lead.get("ejecutivo_asignado") or "?")

    # 2) crm_events con result problemático (propietario_retiro / no_autoriza_gestion)
    for ev in db["crm_events"].find({
        "result": {"$in": ["propietario_retiro", "no_autoriza_gestion"]},
    }, {"lead_id": 1, "result": 1, "notes": 1, "timestamp": 1}):
        lid = ev.get("lead_id")
        lead = db["leads"].find_one({"_id": lid}, {"prospecto": 1, "codigo": 1, "phone": 1, "ejecutivo_asignado": 1}) if lid else None
        if not lead:
            continue
        codigo = _codigo_de(lead)
        motivo = "PROPIETARIO_RETIRO" if ev.get("result") == "propietario_retiro" else "NO_AUTORIZA_GESTION"
        origen = str((lead.get("prospecto") or {}).get("origen") or lead.get("origen") or "S/I")
        prop_motivos[codigo][motivo] += 1
        prop_origenes[codigo][origen] += 1
        prop_ejecutivos[codigo].add(lead.get("ejecutivo_asignado") or "?")

    # 3) Estado de disponibilidad en cartera
    disponible = {}
    for d in db["universo_cartera_prop360"].find({}, {"codigo": 1, "disponible_prop360": 1, "estado": 1}):
        c = str(d.get("codigo") or "")
        if c:
            disponible[c] = {
                "disponible": d.get("disponible_prop360"),
                "estado": d.get("estado"),
            }

    codigos = sorted(prop_motivos.keys(), key=lambda c: -sum(prop_motivos[c].values()))

    if args.json:
        out = []
        for c in codigos:
            out.append({
                "codigo": c,
                "total_notas": sum(prop_motivos[c].values()),
                "motivos": dict(prop_motivos[c]),
                "origenes": dict(prop_origenes[c]),
                "ejecutivos": sorted(prop_ejecutivos[c]),
                "disponible_cartera": disponible.get(c),
                "notas": prop_notas[c],
            })
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return

    print("=" * 100)
    print("REPORTE DE PROPIEDADES PROBLEMÁTICAS (según notas de gestión del CRM)")
    print("=" * 100)
    print(f"\n{len(codigos)} propiedades con señales. Ordenadas por número de señales.\n")
    for c in codigos:
        total = sum(prop_motivos[c].values())
        disp = disponible.get(c)
        disp_txt = "NO DISPONIBLE" if disp and disp.get("disponible") is False else (
            "disponible" if disp else "sin registro en cartera")
        origenes = ", ".join(f"{o} ({n})" for o, n in prop_origenes[c].most_common())
        print(f"* Propiedad {c}  ({total} señales)  [cartera: {disp_txt}]")
        print(f"    Ejecutivos: {', '.join(sorted(prop_ejecutivos[c]))}")
        if origenes:
            print(f"    Origen lead: {origenes}")
        for motivo, n in prop_motivos[c].most_common():
            print(f"    - {motivo}: {n}")
        for nota in prop_notas[c][:3]:
            print(f"      > {nota['fecha']} [{nota['ejecutivo']}] {nota['nota'][:130]}")
        print()


if __name__ == "__main__":
    main()
