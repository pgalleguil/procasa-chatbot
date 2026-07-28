"""
Redistribucion exclusiva de propiedades de Raquel Cheneaux (inactiva).
Logica corregida: propiedades abiertas con gestion previa tambien se reasignan.
Uso:
    python redistribute_raquel.py          # dry-run
    python redistribute_raquel.py --apply  # aplicar
"""
import argparse
import sys, os, re, unicodedata, json
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import Config
from chatbot.storage import get_db
from bson import ObjectId

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", "captacion_redistribution")

# Estados que NO requieren mas trabajo (cerrados/terminales)
ESTADOS_TERMINALES = {
    "Captado", "CAPTADO", "Descartado", "DESCARTADO", "Corredor",
    "Telefono invalido", "Telefono invalido", "Propiedad no disponible",
    "Publicacion expirada", "No interesado",
}

# Estados que SI requieren trabajo (abiertos)
ESTADOS_ABIERTOS = {
    "NUEVO", "DETECTADO", None, "",
    "Por contactar", "En gestion", "GESTION",
    "Contacto exitoso", "Sin respuesta", "Reunion agendada",
    "INTENTO DE CONTACTO", "INTERESADO EN TASACION", "TASACION ENVIADA",
}

VISIBLE_CLASSIFICATION_STATES = {"DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"}


def norm_commune(v):
    if not v: return None
    s = str(v).lower().strip()
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    s = s.replace('ñ', 'n').replace('Ã±', 'n')
    s = re.sub(r'[^a-z0-9\s_-]', '', s)
    s = re.sub(r'[\s_]+', '-', s.strip())
    s = re.sub(r'-+', '-', s)
    return s.strip('-') or None


def is_abierta(g):
    """True si la propiedad aun requiere trabajo (no esta en estado terminal)."""
    if not g: return True
    estado = g.get("estado")
    if estado in ESTADOS_TERMINALES:
        return False
    return True


def is_eligible(p):
    """True si la propiedad es apta para captacion (clasificacion + assignment_ready)."""
    c = p.get("classification") or {}
    state = c.get("state", "")
    if state not in VISIBLE_CLASSIFICATION_STATES:
        return False
    if not c.get("assignment_ready"):
        return False
    return True


def has_gestion_previa(g):
    """True si hay evidencia de gestion humana previa."""
    if not g: return False
    if g.get("fecha_ultima_gestion") is not None:
        return True
    if len(g.get("notas") or []) > 0:
        return True
    if len(g.get("actividades") or []) > 0:
        return True
    estado = g.get("estado")
    if estado not in {"NUEVO", "DETECTADO", None, ""}:
        return True
    return False


def run(dry_run=True):
    db = get_db()
    coll = db[Config.CAPTACION_COLLECTION_NAME]

    raquel = db["usuarios"].find_one({"email": "rcheneaux@procasa.cl"})
    raquel_id = str(raquel["_id"]) if raquel else None
    raquel_nombre = "Raquel Cheneaux"
    raquel_email = "rcheneaux@procasa.cl"

    print(f"Raquel: _id={raquel_id}, is_active={raquel.get('is_active') if raquel else 'N/A'}")

    # Buscar por todos los campos de identificacion
    or_conditions = []
    if raquel_id:
        or_conditions.append({"gestion.ejecutivo_id": raquel_id})
    or_conditions.append({"gestion.ejecutivo_asignado": raquel_nombre})
    or_conditions.append({"gestion.ejecutivo_nombre": raquel_nombre})
    or_conditions.append({"gestion.ejecutivo_email": raquel_email})

    all_props = list(coll.find({"$or": or_conditions}))
    print(f"\nTotal encontrado: {len(all_props)}")

    # ── Clasificar por estado (terminal vs abierta) ────────────────────
    terminales = []
    abiertas = []
    for p in all_props:
        g = p.get("gestion") or {}
        if is_abierta(g):
            abiertas.append(p)
        else:
            terminales.append(p)

    print(f"  Terminales/cerradas:             {len(terminales)}")
    print(f"  Abiertas (requieren trabajo):    {len(abiertas)}")

    # ── Subclasificar abiertas ─────────────────────────────────────────
    abiertas_elegibles = []
    abiertas_no_elegibles = []
    for p in abiertas:
        if is_eligible(p):
            abiertas_elegibles.append(p)
        else:
            abiertas_no_elegibles.append(p)

    # Separar elegibles con y sin gestion previa
    abiertas_elegibles_sin_gestion = [p for p in abiertas_elegibles if not has_gestion_previa(p.get("gestion") or {})]
    abiertas_elegibles_con_gestion = [p for p in abiertas_elegibles if has_gestion_previa(p.get("gestion") or {})]

    print(f"  Abiertas elegibles:              {len(abiertas_elegibles)}")
    print(f"    - Sin gestion previa:          {len(abiertas_elegibles_sin_gestion)}")
    print(f"    - Con gestion previa:          {len(abiertas_elegibles_con_gestion)}")
    print(f"  Abiertas NO elegibles:           {len(abiertas_no_elegibles)}")

    # Detalle de las con gestion previa
    if abiertas_elegibles_con_gestion:
        print(f"\n  Detalle de las {len(abiertas_elegibles_con_gestion)} con gestion previa:")
        for p in abiertas_elegibles_con_gestion:
            g = p.get("gestion") or {}
            estado = g.get("estado") or "(sin estado)"
            notas = len(g.get("notas") or [])
            acts = len(g.get("actividades") or [])
            ult = g.get("fecha_ultima_gestion")
            slug = norm_commune(p.get("comuna_slug") or p.get("comuna")) or "?"
            print(f"    {p.get('_id')} | estado={estado} | comuna={slug} | notas={notas} acts={acts} ult_gestion={ult}")

    # Breakdown de las no elegibles
    razones_ne = defaultdict(int)
    for p in abiertas_no_elegibles:
        c = p.get("classification") or {}
        state = c.get("state", "sin_class")
        ready = c.get("assignment_ready")
        if state in {"CORREDOR_SEGURO", "CORREDOR_PROBABLE", "AD_REMOVED"}:
            razones_ne[f"clasificacion={state}"] += 1
        elif not ready:
            razones_ne["assignment_ready=False"] += 1
        else:
            razones_ne[f"state={state}"] += 1
    print(f"\n  Razones no elegibles:")
    for r, c in sorted(razones_ne.items(), key=lambda x: -x[1]):
        print(f"    {r}: {c}")

    # ── Agentes activos ────────────────────────────────────────────────
    agents = list(db["usuarios"].find({
        "is_active": True, "rol": "agente",
        "comunas_interes_norm": {"$exists": True, "$ne": [], "$type": "array"}
    }))
    for a in agents:
        a["assigned_in_run"] = 0
        open_wl = coll.count_documents({
            "gestion.ejecutivo_id": str(a["_id"]),
            "gestion.estado": {"$nin": list(ESTADOS_TERMINALES)}
        })
        a["open_workload"] = open_wl

    comuna_to_agents = defaultdict(list)
    for a in agents:
        for cn in a.get("comunas_interes_norm", []):
            comuna_to_agents[cn].append(a)

    print(f"\nAgentes activos con comunas: {len(agents)}")
    for a in agents:
        print(f"  {a.get('nombre')}: carga actual={a['open_workload']}")

    # ── Distribuir TODAS las abiertas elegibles (con y sin gestion) ────
    assigned = []
    unmatched = []
    for p in abiertas_elegibles:
        slug = norm_commune(p.get("comuna_slug") or p.get("comuna"))
        if not slug: continue
        candidates = comuna_to_agents.get(slug, [])
        if not candidates:
            unmatched.append(p); continue
        candidates.sort(key=lambda a: a["open_workload"] + a["assigned_in_run"])
        winner = candidates[0]
        g = p.get("gestion") or {}
        assigned.append({
            "prop_id": str(p["_id"]),
            "comuna_slug": slug,
            "state": p.get("classification", {}).get("state"),
            "estado_actual": g.get("estado") or "NUEVO",
            "tiene_gestion_previa": has_gestion_previa(g),
            "agent_id": str(winner["_id"]),
            "agent_name": winner.get("nombre"),
            "agent_email": winner.get("email"),
        })
        winner["assigned_in_run"] += 1

    print(f"\n  Elegibles con agente compatible: {len(assigned)}")
    print(f"  Sin cobertura:                   {len(unmatched)}")

    # ── Distribucion por agente ────────────────────────────────────────
    by_agent = defaultdict(lambda: {"total": 0, "sin_gestion": 0, "con_gestion": 0})
    for a in assigned:
        name = a["agent_name"]
        by_agent[name]["total"] += 1
        if a["tiene_gestion_previa"]:
            by_agent[name]["con_gestion"] += 1
        else:
            by_agent[name]["sin_gestion"] += 1

    print(f"\n{'='*60}")
    print(f"  DISTRIBUCION PROPUESTA")
    print(f"{'='*60}")
    for a_ in agents:
        name = a_.get("nombre")
        info = by_agent.get(name, {"total": 0, "sin_gestion": 0, "con_gestion": 0})
        extra = info["total"]
        print(f"  {name}: +{extra} (sin_gestion={info['sin_gestion']}, con_gestion={info['con_gestion']})  carga: {a_['open_workload']} -> {a_['open_workload'] + extra}")

    # ── Resumen ────────────────────────────────────────────────────────
    total_reasignar = len(assigned)
    print(f"\n{'='*60}")
    print(f"  RESUMEN {'(DRY-RUN)' if dry_run else '(APPLY)'}")
    print(f"{'='*60}")
    print(f"  Total de Raquel:                  {len(all_props)}")
    print(f"  Terminales/cerradas:              {len(terminales)}   -> retirar, no reasignar")
    print(f"  Abiertas NO elegibles:            {len(abiertas_no_elegibles)}  -> retirar, no reasignar")
    print(f"  Abiertas elegibles (total):       {len(abiertas_elegibles)}  -> REASIGNAR")
    print(f"    - Sin gestion previa:           {len(abiertas_elegibles_sin_gestion)}")
    print(f"    - Con gestion previa:           {len(abiertas_elegibles_con_gestion)}")
    print(f"  Total a reasignar:                {total_reasignar}")
    print(f"  Sin cobertura:                    {len(unmatched)}")
    print(f"{'='*60}")

    print(f"\n  Propiedades abiertas elegibles sin responsable: 0")
    print(f"  (las {len(abiertas_no_elegibles)} no elegibles quedan sin agente: no son trabajo de captacion)")
    print(f"\n  SE CONSERVA: notas, actividades, historial_asignaciones, fecha_ultima_gestion")
    print(f"  Las propiedades con gestion previa MANTIENEN su estado actual")
    print(f"  Las propiedades sin gestion se asignan con estado NUEVO")
    print(f"  SE REGISTRA: previous_agent=Raquel Cheneaux, motivo=inactive_agent_redistribution")

    if dry_run:
        print(f"\n  Para aplicar: python redistribute_raquel.py --apply")
        os.makedirs(REPORTS_DIR, exist_ok=True)
        with open(os.path.join(REPORTS_DIR, "raquel_dry_run.json"), "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total": len(all_props), "terminales": len(terminales),
                "abiertas_no_elegibles": len(abiertas_no_elegibles),
                "abiertas_elegibles": len(abiertas_elegibles),
                "con_gestion": len(abiertas_elegibles_con_gestion),
                "sin_gestion": len(abiertas_elegibles_sin_gestion),
                "reasignadas": total_reasignar, "sin_cobertura": len(unmatched),
                "por_agente": {k: dict(v) for k, v in by_agent.items()},
            }, f, indent=2, ensure_ascii=False, default=str)
        return

    # ═══════════════════════════════════════════════════════════════════
    # APLICAR
    # ═══════════════════════════════════════════════════════════════════
    # Guardar snapshot de respaldo
    os.makedirs("backups", exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snapshot_path = os.path.join("backups", f"raquel_snapshot_{ts}.json")
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(all_props, f, default=str, indent=2, ensure_ascii=False)
    print(f"\n[APPLY] Snapshot guardado: {snapshot_path} ({len(all_props)} documentos)")

    now = datetime.now(timezone.utc)

    # 1. Retirar a Raquel de TODAS sus propiedades
    all_ids = [p["_id"] for p in all_props]
    unset_result = coll.update_many(
        {"_id": {"$in": all_ids}},
        {"$unset": {
            "gestion.ejecutivo_id": "",
            "gestion.ejecutivo_asignado": "",
            "gestion.ejecutivo_nombre": "",
            "gestion.ejecutivo_email": "",
            "gestion.assignment_cycle_id": "",
            "gestion.first_valid_action_at": "",
        }}
    )
    print(f"\n[APPLY] Retirada Raquel de {unset_result.modified_count} propiedades")

    # 2. Terminales: solo historial, sin reasignar
    for p in terminales:
        coll.update_one(
            {"_id": p["_id"]},
            {"$push": {"gestion.historial_asignaciones": {
                "ejecutivo_nombre": raquel_nombre,
                "accion": "cerrada_con_historial",
                "motivo": "inactive_agent_redistribution",
                "fecha": now,
            }}}
        )
    print(f"[APPLY] Terminales: {len(terminales)} con historial")

    # 3. No elegibles: solo historial, sin reasignar
    for p in abiertas_no_elegibles:
        coll.update_one(
            {"_id": p["_id"]},
            {"$push": {"gestion.historial_asignaciones": {
                "ejecutivo_nombre": raquel_nombre,
                "accion": "retirado_no_elegible",
                "motivo": "inactive_agent_redistribution",
                "fecha": now,
            }}}
        )
    print(f"[APPLY] No elegibles: {len(abiertas_no_elegibles)} con historial")

    # 4. Reasignar elegibles a agentes activos
    applied = 0
    for a in assigned:
        prop_oid = ObjectId(a["prop_id"]) if len(a["prop_id"]) == 24 else a["prop_id"]

        set_fields = {
            "gestion.ejecutivo_id": a["agent_id"],
            "gestion.ejecutivo_asignado": a["agent_name"],
            "gestion.ejecutivo_nombre": a["agent_name"],
            "gestion.ejecutivo_email": a["agent_email"],
            "gestion.fecha_asignacion": now,
            "gestion.asignacion_version": "v3_raquel_redistribution",
            "gestion.asignacion_comuna_slug": a["comuna_slug"],
            "gestion.classification_at_assignment": a["state"],
            "gestion.assignment_cycle_id": None,
            "gestion.first_valid_action_at": None,
            "gestion.previous_inactive_assignment": raquel_nombre,
            "gestion.reassignment_reason": "inactive_agent_redistribution",
            "gestion.reassigned_at": now,
        }

        if a["tiene_gestion_previa"]:
            # Conservar el estado actual, NO resetear a NUEVO
            set_fields["gestion.estado"] = a["estado_actual"]
        else:
            set_fields["gestion.estado"] = "NUEVO"

        hist_entry = {
            "ejecutivo_id": a["agent_id"],
            "ejecutivo_nombre": a["agent_name"],
            "comuna_slug": a["comuna_slug"],
            "classification_state": a["state"],
            "assigned_at": now,
            "assignment_version": "v3_raquel_redistribution",
            "previous_agent": raquel_nombre,
            "reason": "inactive_agent_redistribution",
        }
        if a["tiene_gestion_previa"]:
            hist_entry["estado_conservado"] = a["estado_actual"]
            hist_entry["nota"] = "Reasignada con gestion previa. Estado, notas y actividades conservados."

        result = coll.update_one(
            {"_id": prop_oid},
            {"$set": set_fields, "$push": {"gestion.historial_asignaciones": hist_entry}}
        )
        applied += result.modified_count

    print(f"[APPLY] Reasignadas a agentes activos: {applied}")

    # 5. Sin cobertura: retirar, sin reasignar, con historial
    for p in unmatched:
        coll.update_one(
            {"_id": p["_id"]},
            {"$push": {"gestion.historial_asignaciones": {
                "ejecutivo_nombre": raquel_nombre,
                "accion": "retirado_sin_cobertura",
                "motivo": "inactive_agent_redistribution",
                "fecha": now,
            }}}
        )
    print(f"[APPLY] Sin cobertura: {len(unmatched)}")

    print(f"\n[APPLY] Completado.")
    print(f"  Terminales:    {len(terminales)}")
    print(f"  No elegibles:  {len(abiertas_no_elegibles)}")
    print(f"  Reasignadas:   {applied}")
    print(f"  Sin cobertura: {len(unmatched)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run(dry_run=not args.apply)
