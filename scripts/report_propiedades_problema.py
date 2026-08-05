"""Reporte de propiedades problemáticas: distingue notas personales de
registros formales, y detecta códigos fantasma.

Cómo registran los ejecutivos (y dónde):
  - sticky_notes:            NOTA PERSONAL (texto libre, no es una respuesta
                             formal del CRM; no sirve para acreditar la
                             propiedad como no disponible).
  - crm_events.result:       RESPUESTA FORMAL (resultado_gestion: lead_cerrado,
                             propietario_retiro, no_autoriza_gestion, etc.).
  - crm_management_results:  RESPUESTA FORMAL (result_type canónico).

El reporte agrupa por código de propiedad y muestra:
  - señales en notas personales  -> solo aviso, NO formal
  - registros formales           -> válidos para suspender la propiedad
  - estado en cartera            -> disponible / no disponible / fantasma

Uso:
  python scripts/report_propiedades_problema.py            # consola
  python scripts/report_propiedades_problema.py --json     # JSON
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
        r"fuera de la venta", r"no se encuentra disponible", r"no disponible segun",
    ]),
    ("VENDIDA_O_ARRIENDADA", [
        r"\bvendid[oa]\b", r"vendido hace", r"vendida hace", r"\barrendad[oa]\b",
        r"\barrendad[oa]\b hace", r"\barrendad[oa]\b",
    ]),
    ("PROPIETARIO_RETIRO", [
        r"propietario_retiro", r"retir[oa]d[oa]", r"propietari[oa].*retir", r"dueñ[oa].*retir",
        r"ya avise que no esta a la venta", r"ya no esta a la venta",
    ]),
    ("PROPIETARIO_NO_ATIENDE", [
        r"no atiende", r"no responde", r"no contesta", r"fuera de servicio",
        r"no me puedo comunicar", r"no puedo comunicarme", r"no se puede contactar",
        r"no se puede comunicar", r"aun no responde", r"sin respuesta",
    ]),
    ("DATOS_NO_EXISTEN", [
        r"no existe", r"datos no existen", r"datos del due", r"no me arroja", r"no arroja",
        r"informaci[oó]n inexistente", r"ficha inexistente", r"mal ingresado",
        r"número telefónico mal ingresado", r"numero telefonico mal ingresado",
        r"telefono.*no.*existe", r"numero invalido", r"tel[eé]fono inv[áa]lido",
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
    ap = argparse.ArgumentParser(description="Propiedades problemáticas por registros del CRM")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--desde", type=str, default=None, help="YYYY-MM-DD (fecha del registro)")
    args = ap.parse_args()

    db = get_db()
    desde = None
    if args.desde:
        desde = datetime.fromisoformat(args.desde)

    # { codigo: { "notas": Counter, "formales": Counter, "origenes": Counter,
    #            "ejecutivos": set, "detalle_notas": [...], "detalle_formales": [...] } }
    props = defaultdict(lambda: {
        "notas": Counter(), "formales": Counter(), "origenes": Counter(),
        "ejecutivos": set(), "detalle_notas": [], "detalle_formales": [],
    })

    def _fecha_ok(fecha) -> bool:
        if not desde or not fecha:
            return True
        try:
            return datetime.fromisoformat(str(fecha)) >= desde
        except ValueError:
            return True

    # 1) NOTAS PERSONALES (sticky_notes)
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
            if not _fecha_ok(fecha):
                continue
            props[codigo]["notas"][motivo] += 1
            props[codigo]["origenes"][origen] += 1
            props[codigo]["ejecutivos"].add(lead.get("ejecutivo_asignado") or "?")
            props[codigo]["detalle_notas"].append({
                "tipo": "nota_personal", "motivo": motivo, "fecha": fecha,
                "phone": lead.get("phone"), "origen": origen,
                "ejecutivo": lead.get("ejecutivo_asignado"), "texto": content,
            })

    # 2) RESPUESTAS FORMALES vía crm_events.result
    for ev in db["crm_events"].find({"result": {"$in": [
        "propietario_retiro", "no_autoriza_gestion", "lead_cerrado", "no_logra_contacto",
    ]}}, {"lead_id": 1, "result": 1, "notes": 1, "timestamp": 1}):
        lid = ev.get("lead_id")
        if not lid:
            continue
        lead = db["leads"].find_one({"_id": lid}, {"prospecto": 1, "codigo": 1, "phone": 1, "ejecutivo_asignado": 1})
        if not lead:
            continue
        codigo = _codigo_de(lead)
        origen = str((lead.get("prospecto") or {}).get("origen") or lead.get("origen") or "S/I")
        motivo = {
            "propietario_retiro": "PROPIETARIO_RETIRO",
            "no_autoriza_gestion": "NO_AUTORIZA_GESTION",
            "lead_cerrado": "LEAD_CERRADO",
            "no_logra_contacto": "PROPIETARIO_NO_ATIENDE",
        }.get(ev.get("result"), str(ev.get("result")).upper())
        fecha = ev.get("timestamp")
        if not _fecha_ok(fecha):
            continue
        props[codigo]["formales"][motivo] += 1
        props[codigo]["origenes"][origen] += 1
        props[codigo]["ejecutivos"].add(lead.get("ejecutivo_asignado") or "?")
        props[codigo]["detalle_formales"].append({
            "tipo": "resultado_gestion", "motivo": motivo, "fecha": fecha,
            "phone": lead.get("phone"), "origen": origen,
            "ejecutivo": lead.get("ejecutivo_asignado"),
            "texto": ev.get("notes") or "",
        })

    # 3) RESPUESTAS FORMALES vía crm_management_results (result_type canónico)
    for r in db["crm_management_results"].find({}, {
        "lead_id": 1, "result_type": 1, "occurred_at": 1,
    }):
        lid = r.get("lead_id")
        if not lid:
            continue
        lead = db["leads"].find_one({"_id": lid}, {"prospecto": 1, "codigo": 1, "phone": 1, "ejecutivo_asignado": 1})
        if not lead:
            continue
        codigo = _codigo_de(lead)
        origen = str((lead.get("prospecto") or {}).get("origen") or lead.get("origen") or "S/I")
        fecha = r.get("occurred_at")
        if not _fecha_ok(fecha):
            continue
        props[codigo]["formales"][str(r.get("result_type"))] += 1
        props[codigo]["origenes"][origen] += 1
        props[codigo]["ejecutivos"].add(lead.get("ejecutivo_asignado") or "?")
        props[codigo]["detalle_formales"].append({
            "tipo": "management_result", "motivo": str(r.get("result_type")), "fecha": fecha,
            "phone": lead.get("phone"), "origen": origen,
            "ejecutivo": lead.get("ejecutivo_asignado"), "texto": "",
        })

    # 4) Estado en cartera
    disponible = {}
    for d in db["universo_cartera_prop360"].find({}, {"codigo": 1, "disponible_prop360": 1, "estado": 1}):
        c = str(d.get("codigo") or "")
        if c:
            disponible[c] = {"disponible": d.get("disponible_prop360"), "estado": d.get("estado")}

    codigos = sorted(props.keys(), key=lambda c: -(
        sum(props[c]["notas"].values()) + sum(props[c]["formales"].values())))

    if args.json:
        out = []
        for c in codigos:
            d = props[c]
            out.append({
                "codigo": c,
                "notas_personales": dict(d["notas"]),
                "registros_formales": dict(d["formales"]),
                "total_notas": sum(d["notas"].values()),
                "total_formales": sum(d["formales"].values()),
                "origenes": dict(d["origenes"]),
                "ejecutivos": sorted(d["ejecutivos"]),
                "disponible_cartera": disponible.get(c),
                "detalle_notas": d["detalle_notas"],
                "detalle_formales": d["detalle_formales"],
            })
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return

    print("=" * 100)
    print("REPORTE DE PROPIEDADES PROBLEMÁTICAS")
    print("nota personal (sticky_notes) ≠ respuesta formal (resultado_gestion/management)")
    print("=" * 100)
    solo_fantasma = [c for c in codigos if c not in disponible]
    solo_formal = [c for c in codigos if props[c]["formales"] and not props[c]["notas"]]
    mixto = [c for c in codigos if props[c]["formales"] and props[c]["notas"]]
    solo_nota = [c for c in codigos if props[c]["notas"] and not props[c]["formales"]]

    print(f"\n{len(codigos)} propiedades con señales:")
    print(f"  - solo NOTA personal (sin respuesta formal): {len(solo_nota)}")
    print(f"  - con respuesta FORMAL: {len(solo_formal + mixto)}")
    print(f"  - código FANTASMA (no existe en cartera): {len(solo_fantasma)}")

    print("\n" + "=" * 100)
    print("CÓDIGOS A REGULARIZAR / SUSPENDER (con respuesta FORMAL o en cartera disponible)")
    print("=" * 100)
    for c in codigos:
        d = props[c]
        if not d["formales"]:
            continue
        disp = disponible.get(c)
        disp_txt = "NO DISPONIBLE" if disp and disp.get("disponible") is False else (
            "DISPONIBLE ⚠️" if disp else "FANTASMA ⚠️")
        total = sum(d["notas"].values()) + sum(d["formales"].values())
        print(f"* Propiedad {c}  ({total} señales)  [cartera: {disp_txt}]")
        if d["formales"]:
            print(f"    FORMAL: {', '.join(f'{m} ({n})' for m, n in d['formales'].most_common())}")
        if d["notas"]:
            print(f"    notas:  {', '.join(f'{m} ({n})' for m, n in d['notas'].most_common())}")
        orig = ", ".join(f"{o} ({n})" for o, n in d["origenes"].most_common(3))
        if orig:
            print(f"    origen: {orig}")
        for det in d["detalle_formales"][:2]:
            print(f"      > {det['fecha']} [{det['ejecutivo']}] {det['tipo']}: {det['motivo']} — {str(det['texto'])[:90]}")
        print()

    print("=" * 100)
    print("SOLO NOTA PERSONAL (aviso, SIN respuesta formal — feedback al equipo)")
    print("=" * 100)
    for c in codigos:
        d = props[c]
        if d["formales"]:
            continue
        disp = disponible.get(c)
        disp_txt = "NO DISPONIBLE" if disp and disp.get("disponible") is False else (
            "DISPONIBLE ⚠️" if disp else "FANTASMA ⚠️")
        total = sum(d["notas"].values())
        print(f"* Propiedad {c}  ({total} señales)  [cartera: {disp_txt}]")
        print(f"    notas: {', '.join(f'{m} ({n})' for m, n in d['notas'].most_common())}")
        orig = ", ".join(f"{o} ({n})" for o, n in d["origenes"].most_common(3))
        if orig:
            print(f"    origen: {orig}")
        for det in d["detalle_notas"][:2]:
            print(f"      > {det['fecha']} [{det['ejecutivo']}] {det['texto'][:110]}")
        print()


if __name__ == "__main__":
    main()
