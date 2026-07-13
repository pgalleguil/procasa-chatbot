"""
assign_captacion_properties.py
Distribuye propiedades Toctoc DUEÑO_SEGURO e INCIERTO entre agentes activos
según comunas de interés con distribución ponderada.

Uso:
    python assign_captacion_properties.py --dry-run
    python assign_captacion_properties.py --apply
"""
import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import Config
from chatbot.storage import get_db

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

TERMINAL_STATES = {"Captado", "CAPTADO", "Descartado", "DESCARTADO", "Corredor",
                   "Teléfono inválido", "Propiedad no disponible", "Publicación expirada", "No interesado"}

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", "captacion_assignment")


def normalize_commune_canonical(value):
    if not value:
        return None
    import re
    import unicodedata
    s = str(value).lower().strip()
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    s = s.replace('ñ', 'n').replace('Ã±', 'n')
    s = re.sub(r'[^a-z0-9\s_-]', '', s)
    s = re.sub(r'[\s_]+', '-', s.strip())
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    return s if s else None


def get_elegible_agents(db):
    """Retorna agentes activos con comunas_interes_norm poblado."""
    users = list(db["usuarios"].find({
        "is_active": True,
        "rol": "agente",
        "comunas_interes_norm": {"$exists": True, "$ne": [], "$type": "array"}
    }))
    for u in users:
        u["captacion_weight"] = u.get("captacion_weight", 1.0) or 1.0
        u["assigned_in_run"] = 0
        # Carga existente
        open_wl = db[Config.CAPTACION_COLLECTION_NAME].count_documents({
            "origen": "toctoc",
            "gestion.ejecutivo_id": str(u["_id"]),
            "gestion.estado": {"$nin": list(TERMINAL_STATES)}
        })
        u["open_workload"] = open_wl
    return users


def compute_load_score(agent):
    return (agent["open_workload"] + agent["assigned_in_run"]) / agent["captacion_weight"]


def run_distribution(dry_run=True):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    db = get_db()
    coll = db[Config.CAPTACION_COLLECTION_NAME]
    
    # 1. Agentes elegibles
    agents = get_elegible_agents(db)
    logger.info(f"Agentes elegibles: {len(agents)}")
    for a in agents:
        logger.info(f"  {a.get('nombre')} (weight={a['captacion_weight']}, workload={a['open_workload']})")
    
    # Mapa comuna_slug -> agentes
    comuna_to_agents = defaultdict(list)
    for a in agents:
        for c_norm in a.get("comunas_interes_norm", []):
            comuna_to_agents[c_norm].append(a)
    
    # 2. Propiedades objetivo
    target_query = {
        "origen": "toctoc",
        "classification.state": {"$in": ["DUEÑO_SEGURO", "INCIERTO"]},
        "$or": [
            {"gestion.ejecutivo_id": {"$exists": False}},
            {"gestion.ejecutivo_id": None},
        ]
    }
    
    all_targets = list(coll.find(target_query))
    logger.info(f"Propiedades objetivo: {len(all_targets)}")
    
    # Separar por estado
    dueno_seguro = [p for p in all_targets if p.get("classification", {}).get("state") == "DUEÑO_SEGURO"]
    incierto = [p for p in all_targets if p.get("classification", {}).get("state") == "INCIERTO"]
    logger.info(f"  DUEÑO_SEGURO: {len(dueno_seguro)}")
    logger.info(f"  INCIERTO: {len(incierto)}")
    
    # 3. Verificar comuna_slug
    props_sin_comuna = [p for p in all_targets if not normalize_commune_canonical(p.get("comuna_slug") or p.get("comuna"))]
    logger.info(f"  Sin comuna_slug: {len(props_sin_comuna)}")
    
    # 4. Estadísticas de comunas
    comuna_counts = defaultdict(int)
    for p in all_targets:
        slug = normalize_commune_canonical(p.get("comuna_slug") or p.get("comuna"))
        if slug:
            comuna_counts[slug] += 1
    
    # 5. Propuestas de asignación
    assigned = []  # [{prop_id, comuna_slug, state, agent_id, agent_name, agent_email}]
    unmatched_communes = set()
    unmatched_count = 0
    
    # Procesar DUEÑO_SEGURO primero, luego INCIERTO
    for state_group in [dueno_seguro, incierto]:
        for p in state_group:
            slug = normalize_commune_canonical(p.get("comuna_slug") or p.get("comuna"))
            if not slug:
                continue
            
            candidates = comuna_to_agents.get(slug, [])
            if not candidates:
                # Try broader match: check if any agent has a commune that normalizes to the same
                for c_norm, agent_list in comuna_to_agents.items():
                    if c_norm == slug:
                        candidates = agent_list
                        break
                if not candidates:
                    unmatched_communes.add(slug)
                    unmatched_count += 1
                    continue
            
            # Elegir el de menor load_score
            candidates.sort(key=lambda a: compute_load_score(a))
            winner = candidates[0]
            
            assigned.append({
                "prop_id": str(p["_id"]),
                "comuna_slug": slug,
                "state": p.get("classification", {}).get("state"),
                "agent_id": str(winner["_id"]),
                "agent_name": winner.get("nombre"),
                "agent_email": winner.get("email"),
            })
            winner["assigned_in_run"] += 1
    
    # 6. Compilar reporte
    report = {
        "dry_run": dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agents_total": len(agents),
        "agents": [
            {
                "id": str(a["_id"]),
                "nombre": a.get("nombre"),
                "email": a.get("email"),
                "comunas_interes": a.get("comunas_interes"),
                "comunas_interes_norm": a.get("comunas_interes_norm"),
                "weight": a["captacion_weight"],
                "open_workload_before": a["open_workload"],
                "assigned_this_run": a["assigned_in_run"],
                "projected_workload": a["open_workload"] + a["assigned_in_run"],
            }
            for a in agents
        ],
        "target_properties": len(all_targets),
        "dueno_seguro_count": len(dueno_seguro),
        "incierto_count": len(incierto),
        "already_assigned": 0,
        "sin_comuna_slug": len(props_sin_comuna),
        "unmatched_communes": sorted(list(unmatched_communes)),
        "unmatched_count": unmatched_count,
        "proposed_assignments": len(assigned),
        "distribution_by_agent": {},
        "distribution_by_state": {"DUEÑO_SEGURO": {}, "INCIERTO": {}},
        "distribution_by_commune": {},
        "max_load_diff": 0,
        "yapo_affected": 0,
    }
    
    # Por agente
    agent_stats = defaultdict(lambda: {"total": 0, "dueno": 0, "incierto": 0})
    for a in assigned:
        agent_stats[a["agent_name"]]["total"] += 1
        if a["state"] == "DUEÑO_SEGURO":
            agent_stats[a["agent_name"]]["dueno"] += 1
        else:
            agent_stats[a["agent_name"]]["incierto"] += 1
    report["distribution_by_agent"] = {
        name: dict(stats) for name, stats in agent_stats.items()
    }
    
    # Por comuna
    comuna_dist = defaultdict(int)
    for a in assigned:
        comuna_dist[a["comuna_slug"]] += 1
    report["distribution_by_commune"] = dict(comuna_dist)
    
    # Por estado por agente
    for a in assigned:
        state = a["state"]
        name = a["agent_name"]
        if state not in report["distribution_by_state"]:
            report["distribution_by_state"][state] = {}
        report["distribution_by_state"][state][name] = report["distribution_by_state"][state].get(name, 0) + 1
    
    # Diferencia de carga
    workloads = [a["open_workload"] + a["assigned_in_run"] for a in agents]
    report["max_load_diff"] = max(workloads) - min(workloads) if workloads else 0
    
    # Guardar reports
    report_path = os.path.join(REPORTS_DIR, "dry_run.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, default=str, indent=2, ensure_ascii=False)
    
    proposals_path = os.path.join(REPORTS_DIR, "proposed_assignments.jsonl")
    with open(proposals_path, "w", encoding="utf-8") as f:
        for a in assigned:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    
    unmatched_path = os.path.join(REPORTS_DIR, "unmatched_communes.json")
    with open(unmatched_path, "w", encoding="utf-8") as f:
        json.dump(sorted(list(unmatched_communes)), f, indent=2, ensure_ascii=False)
    
    # Markdown report
    md = []
    md.append("# Reporte de Asignación de Captaciones\n")
    md.append(f"**Modo:** {'DRY-RUN' if dry_run else 'APPLY'}")
    md.append(f"**Timestamp:** {report['timestamp']}\n")
    md.append("## Resumen\n")
    md.append(f"| Métrica | Valor |")
    md.append(f"|---------|-------|")
    md.append(f"| Agentes activos totales | {len(list(db['usuarios'].find({'is_active': True, 'rol': 'agente'})))} |")
    md.append(f"| Agentes elegibles (con comunas) | {len(agents)} |")
    md.append(f"| Propiedades objetivo | {report['target_properties']} |")
    md.append(f"| DUEÑO_SEGURO | {report['dueno_seguro_count']} |")
    md.append(f"| INCIERTO | {report['incierto_count']} |")
    md.append(f"| Ya asignadas | {report['already_assigned']} |")
    md.append(f"| Sin comuna_slug | {report['sin_comuna_slug']} |")
    md.append(f"| Sin agente compatible | {report['unmatched_count']} |")
    md.append(f"| Propuestas de asignación | {report['proposed_assignments']} |")
    md.append(f"| Documentos Yapo afectados | {report['yapo_affected']} |")
    md.append(f"| Diferencia máxima de carga | {report['max_load_diff']} |\n")
    
    md.append("## Distribución por agente\n")
    md.append("| Agente | DUEÑO_SEGURO | INCIERTO | Total | Carga previa | Carga proyectada | Peso |")
    md.append("|--------|-------------|---------|-------|-------------|-----------------|------|")
    for a in report["agents"]:
        stats = agent_stats.get(a["nombre"], {"total": 0, "dueno": 0, "incierto": 0})
        md.append(f"| {a['nombre']} | {stats['dueno']} | {stats['incierto']} | {stats['total']} | {a['open_workload_before']} | {a['projected_workload']} | {a['weight']} |")
    
    md.append("\n## Distribución por comuna\n")
    md.append("| Comuna | Asignaciones |")
    md.append("|--------|-------------|")
    for comm, cnt in sorted(comuna_dist.items()):
        md.append(f"| {comm} | {cnt} |")
    
    md.append("\n## Comunas sin agente compatible\n")
    if unmatched_communes:
        for c in sorted(unmatched_communes):
            md.append(f"- {c}")
    else:
        md.append("(ninguna)")
    
    md_path = os.path.join(REPORTS_DIR, "dry_run.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    
    # Print summary
    print(f"\n{'='*60}")
    print(f"  REPORTE DE ASIGNACIÓN ({'DRY-RUN' if dry_run else 'APPLY'})")
    print(f"{'='*60}")
    print(f"  Agentes elegibles:     {len(agents)}")
    print(f"  Propiedades objetivo:  {report['target_properties']}")
    print(f"  DUEÑO_SEGURO:          {report['dueno_seguro_count']}")
    print(f"  INCIERTO:              {report['incierto_count']}")
    print(f"  Sin comuna_slug:       {report['sin_comuna_slug']}")
    print(f"  Sin agente:            {report['unmatched_count']}")
    print(f"  Propuestas:            {report['proposed_assignments']}")
    print(f"  Yapo afectados:        {report['yapo_affected']}")
    print(f"  Diferencia carga:      {report['max_load_diff']}")
    print(f"{'='*60}\n")
    
    if not dry_run and assigned:
        apply_assignments(db, coll, assigned, report)
    
    return report


def apply_assignments(db, coll, assignments, report):
    """Aplica las asignaciones con escritura atómica."""
    from bson import ObjectId
    now = datetime.now(timezone.utc)
    applied = 0
    errors = 0
    
    for a in assignments:
        prop_oid = ObjectId(a["prop_id"]) if len(a["prop_id"]) == 24 else a["prop_id"]
        result = coll.update_one(
            {
                "_id": prop_oid,
                "origen": "toctoc",
                "$or": [
                    {"gestion.ejecutivo_id": {"$exists": False}},
                    {"gestion.ejecutivo_id": None},
                ]
            },
            {"$set": {
                "gestion.ejecutivo_id": a["agent_id"],
                "gestion.ejecutivo_nombre": a["agent_name"],
                "gestion.ejecutivo_email": a["agent_email"],
                "gestion.ejecutivo_asignado": a["agent_name"],
                "gestion.fecha_asignacion": now,
                "gestion.asignacion_version": "v1_weighted_commune",
                "gestion.asignacion_comuna_slug": a["comuna_slug"],
                "gestion.classification_at_assignment": a["state"],
                "gestion.assignment_weight": 1.0,
                "gestion.estado": "NUEVO",
                "gestion.historial_asignaciones": [{
                    "ejecutivo_id": a["agent_id"],
                    "ejecutivo_nombre": a["agent_name"],
                    "comuna_slug": a["comuna_slug"],
                    "classification_state": a["state"],
                    "assigned_at": now,
                    "assignment_version": "v1_weighted_commune"
                }]
            }}
        )
        if result.modified_count > 0 or result.matched_count > 0:
            applied += 1
        else:
            errors += 1
    
    report["applied"] = applied
    report["errors"] = errors
    logger.info(f"Asignaciones aplicadas: {applied}, errores: {errors}")


def assign_new_captaciones(run_id=None):
    """Función pública para asignar propiedades nuevas post-scraping."""
    return run_distribution(dry_run=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Distribuir captaciones entre agentes")
    parser.add_argument("--dry-run", action="store_true", help="Solo simular sin escribir")
    parser.add_argument("--apply", action="store_true", help="Aplicar distribución")
    args = parser.parse_args()
    
    dry_run = args.dry_run or not args.apply
    report = run_distribution(dry_run=dry_run)
    
    if dry_run:
        print(f"\nReportes guardados en: {REPORTS_DIR}/")
        print("Revisa los archivos dry_run.json, dry_run.md, proposed_assignments.jsonl, unmatched_communes.json")
        print("\nPara aplicar: python assign_captacion_properties.py --apply")
