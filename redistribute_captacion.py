"""
Redistribucion general de propiedades elegibles sin gestionar.
Agentes activos, comunas de interes, balance por stock_pendiente_sin_gestion.

Metrica canonica: stock_pendiente_sin_gestion = propiedades abiertas, elegibles,
sin evidencia de gestion (notas, actividades, fecha_ultima_gestion, eventos).

Reglas:
1. Sin gestion: redistribuible entre agentes compatibles por comuna.
2. Con cualquier gestion: protegida, no se mueve automaticamente.
3. Agente que gestiona mas recibe prioridad en nuevas distribuciones.
4. Balance dentro de comunas compartidas, no global forzado.
5. Guarda atomica: verificar sin gestion justo antes de mover.
6. Agente inactivo con gestion: va a revision de supervisor, no redistribucion.

Uso:
    python redistribute_captacion.py --dry-run
    python redistribute_captacion.py --apply
"""
import argparse, json, logging, os, re, sys, unicodedata
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import Config
from chatbot.storage import get_db
from bson import ObjectId

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", "captacion_redistribution")

TERMINAL_STATES = {"Captado", "CAPTADO", "Descartado", "DESCARTADO", "Corredor",
                   "Telefono invalido", "Propiedad no disponible", "Publicacion expirada", "No interesado"}

SIN_GESTION_ESTADOS = {None, "", "NUEVO", "DETECTADO"}
VISIBLE_CLASSIFICATION_STATES = {"DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO"}


def norm(v):
    if not v: return None
    s = str(v).lower().strip()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.replace("ñ", "n")
    s = re.sub(r"[^a-z0-9\s_-]", "", s)
    s = re.sub(r"[\s_]+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    return s.strip("-") or None


def has_management_evidence(prop, events_coll):
    """Determina si una propiedad tiene evidencia de gestion humana.
    Fuentes: gestion.notas, gestion.actividades, gestion.fecha_ultima_gestion,
    gestion.estado, y captacion_management_events (por property_id, listing_id, url)."""
    g = prop.get("gestion") or {}

    estado = g.get("estado")
    if estado not in SIN_GESTION_ESTADOS:
        return True, f"estado={estado}"

    if g.get("fecha_ultima_gestion") is not None:
        return True, "fecha_ultima_gestion"

    if len(g.get("notas") or []) > 0:
        return True, f"notas={len(g['notas'])}"

    if len(g.get("actividades") or []) > 0:
        return True, f"actividades={len(g['actividades'])}"

    if events_coll is not None:
        pid = str(prop.get("_id", ""))
        lid = prop.get("listing_id")
        url = prop.get("url")
        or_conds = []
        if pid: or_conds.append({"property_id": pid})
        if lid: or_conds.append({"listing_id": lid})
        if url: or_conds.append({"url": url})
        if or_conds:
            if events_coll.find_one({"$or": or_conds}):
                return True, "management_event"

    return False, None


def is_eligible(prop):
    c = prop.get("classification") or {}
    if c.get("state") not in VISIBLE_CLASSIFICATION_STATES: return False
    if not c.get("assignment_ready"): return False
    if prop.get("scrape_stage") in {"ad_removed", "needs_rescrape", "incomplete"}: return False
    if prop.get("html_validation_status") in {"LISTING_REMOVED", "INVALID", "BLOCKED"}: return False
    if not norm(prop.get("comuna_slug") or prop.get("comuna")): return False
    return True


def stock_pendiente(agent_id, coll, events_coll):
    """Calcula stock pendiente sin gestion: abiertas, elegibles, sin evidencia."""
    count = 0
    for p in coll.find({"gestion.ejecutivo_id": agent_id}):
        g = p.get("gestion") or {}
        if g.get("estado") in TERMINAL_STATES: continue
        if not is_eligible(p): continue
        has_ev, _ = has_management_evidence(p, events_coll)
        if not has_ev:
            count += 1
    return count


def run(dry_run=True):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    db = get_db()
    coll = db[Config.CAPTACION_COLLECTION_NAME]
    events_coll = db["captacion_management_events"]

    # ── Agentes activos ────────────────────────────────────────────────
    agents_raw = list(db["usuarios"].find({
        "is_active": True, "rol": "agente",
        "comunas_interes_norm": {"$exists": True, "$ne": [], "$type": "array"}
    }))
    agents = {}
    for a in agents_raw:
        aid = str(a["_id"])
        agents[aid] = {
            "id": aid, "name": a.get("nombre"), "email": a.get("email"),
            "comunas_norm": a.get("comunas_interes_norm", []),
            "comunas_raw": a.get("comunas_interes", []),
            "weight": a.get("captacion_weight", 1.0) or 1.0,
        }

    # ── Stock pendiente sin gestion actual ──────────────────────────────
    agent_stock = {}
    agent_sg = defaultdict(list)
    agent_managed = defaultdict(list)

    all_assigned = list(coll.find(
        {"gestion.ejecutivo_id": {"$in": list(agents.keys())}},
    ))
    for p in all_assigned:
        aid = (p.get("gestion") or {}).get("ejecutivo_id")
        if aid not in agents: continue
        estado = (p.get("gestion") or {}).get("estado")
        if estado in TERMINAL_STATES: continue
        if not is_eligible(p): continue
        has_ev, _ = has_management_evidence(p, events_coll)
        if has_ev:
            agent_managed[aid].append(p)
        else:
            agent_sg[aid].append(p)

    for aid in agents:
        agent_stock[aid] = len(agent_sg[aid])

    print(f"\n{'='*70}")
    print(f"  STOCK PENDIENTE SIN GESTION POR AGENTE")
    print(f"{'='*70}")
    for aid in sorted(agent_stock, key=lambda x: agent_stock[x]):
        a = agents[aid]
        print(f"  {a['name']:<28} stock_pendiente={agent_stock[aid]:>3}  "
              f"(gestionados={len(agent_managed[aid]):>3})")

    # ── Mapa comuna -> agentes ─────────────────────────────────────────
    comuna_to_agents = defaultdict(list)
    for aid, a in agents.items():
        for cn in a["comunas_norm"]:
            comuna_to_agents[cn].append(aid)

    # ── Pool: elegibles sin gestionar, sin agente ─────────────────────
    all_unassigned = list(coll.find(
        {"$or": [{"gestion.ejecutivo_id": {"$exists": False}}, {"gestion.ejecutivo_id": None}]},
    ))
    pool = []
    for p in all_unassigned:
        if not is_eligible(p): continue
        has_ev, _ = has_management_evidence(p, events_coll)
        if has_ev: continue
        pool.append(p)

    print(f"\n  Pool sin agente (elegibles, sin gestion):  {len(pool)}")

    # ── Agrupar pool por comuna ────────────────────────────────────────
    pool_by_comuna = defaultdict(list)
    for p in pool:
        slug = norm(p.get("comuna_slug") or p.get("comuna"))
        if slug: pool_by_comuna[slug].append(p)

    # ── Distribucion con balance por stock_pendiente ───────────────────
    stock_sim = dict(agent_stock)
    distrib = defaultdict(lambda: {"recibe_pool": 0, "recibe_reasign": 0, "por_comuna": defaultdict(int)})
    sin_cobertura = defaultdict(int)

    # Distribuir pool
    for slug, props in pool_by_comuna.items():
        candidates = comuna_to_agents.get(slug, [])
        if not candidates:
            sin_cobertura[slug] += len(props)
            continue
        for p in props:
            best = min(candidates, key=lambda aid: stock_sim.get(aid, 0))
            stock_sim[best] += 1
            distrib[best]["recibe_pool"] += 1
            distrib[best]["por_comuna"][slug] += 1

    pool_asignadas = sum(d["recibe_pool"] for d in distrib.values())

    # Reasignaciones internas: equilibrar stock_pendiente en comunas compartidas
    reasign_plan = []
    entregadas_por_agente = defaultdict(int)

    for slug, agent_ids in comuna_to_agents.items():
        if len(agent_ids) < 2: continue
        stocks = {aid: stock_sim.get(aid, 0) for aid in agent_ids}
        max_s = max(stocks.values())
        min_s = min(stocks.values())
        if max_s - min_s < 5 or max_s < 10: continue

        overloaded = max(stocks, key=stocks.get)
        sg_in_comuna = []
        for p in agent_sg.get(overloaded, []):
            if norm(p.get("comuna_slug") or p.get("comuna")) == slug:
                has_ev, _ = has_management_evidence(p, events_coll)
                if not has_ev:
                    sg_in_comuna.append(p)

        avg = sum(stocks.values()) / len(stocks)
        to_free = min(len(sg_in_comuna), int(max_s - avg))
        if to_free <= 0: continue

        for i in range(to_free):
            p = sg_in_comuna[i]
            best = min(agent_ids, key=lambda aid: stock_sim.get(aid, 0))
            stock_sim[overloaded] -= 1
            stock_sim[best] += 1
            distrib[best]["recibe_reasign"] += 1
            distrib[best]["por_comuna"][slug] += 1
            entregadas_por_agente[overloaded] += 1
            reasign_plan.append((p, overloaded, best, slug))

    total_reasign = len(reasign_plan)
    reasign_comunas = len(set(r[3] for r in reasign_plan))

    # ── Resultados ─────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  RESUMEN DE DISTRIBUCION {'(DRY-RUN)' if dry_run else '(APPLY)'}")
    print(f"{'='*70}")

    total_gestionadas = sum(len(v) for v in agent_managed.values())

    total_gestionadas = sum(len(v) for v in agent_managed.values())

    print(f"\n  Stock pendiente sin gestion (pool):        {len(pool)}")
    print(f"  Asignadas desde el pool:                    {pool_asignadas}")
    print(f"  Reasignadas entre agentes activos:          {total_reasign}")
    print(f"  Comunas involucradas en la reasignacion:    {reasign_comunas}")
    print(f"  Sin cobertura territorial:                  {sum(sin_cobertura.values())}")

    print(f"\n  {'Agente':<28} {'Stock':>6} {'Entrega':>8} {'Recibe':>7} {'Final':>6}")
    print(f"  {'-'*28} {'-'*6} {'-'*8} {'-'*7} {'-'*6}")
    for aid in sorted(agent_stock, key=lambda x: agent_stock[x]):
        a = agents[aid]
        ini = agent_stock[aid]
        ent = entregadas_por_agente.get(aid, 0)
        rec = distrib[aid]["recibe_pool"] + distrib[aid]["recibe_reasign"]
        fin = stock_sim.get(aid, 0)
        print(f"  {a['name']:<28} {ini:>6} {ent:>8} {rec:>7} {fin:>6}")

    total_recibe = pool_asignadas + total_reasign
    print(f"\n  Total recibido por agentes: {total_recibe}  (pool={pool_asignadas} + reasign={total_reasign})")
    print(f"  Propiedades gestionadas protegidas: {total_gestionadas} (NO se tocan)")

    # ── Detalle Maria Paz y Hernan ────────────────────────────────────
    target_emails = {"mgalleguillos@procasa.cl": "Maria Paz Galleguillos",
                     "h.castroman.8@gmail.com": "Hernan Castro"}
    for email, label in target_emails.items():
        target_aid = next((aid for aid, a in agents.items() if a.get("email") == email), None)
        if target_aid and target_aid in distrib:
            d = distrib[target_aid]
            total = d["recibe_pool"] + d["recibe_reasign"]
            print(f"\n  {label}: recibe {total} de {len(d['por_comuna'])} comunas")
            for slug, cnt in sorted(d["por_comuna"].items(), key=lambda x: -x[1]):
                print(f"    {slug}: {cnt}")

    if sin_cobertura:
        print(f"\n  Principales comunas sin cobertura ({len(sin_cobertura)} total):")
        for slug, cnt in sorted(sin_cobertura.items(), key=lambda x: -x[1])[:20]:
            print(f"    {slug}: {cnt}")

    if dry_run:
        print(f"\n  Para aplicar: python redistribute_captacion.py --apply")
        return

    # ═══════════════════════════════════════════════════════════════════
    # APLICAR
    # ═══════════════════════════════════════════════════════════════════
    now = datetime.now(timezone.utc)

    # Snapshot de todo lo que se va a modificar
    pool_ids = [p["_id"] for p in pool]
    reasign_ids = [r[0]["_id"] for r in reasign_plan]
    all_snapshot_ids = list(set(pool_ids + reasign_ids))
    snapshot_docs = list(coll.find({"_id": {"$in": all_snapshot_ids}}))
    ts = now.strftime("%Y%m%d_%H%M%S")
    snapshot_path = os.path.join("backups", f"captacion_redist_{ts}.json")
    os.makedirs("backups", exist_ok=True)
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(snapshot_docs, f, default=str, indent=2, ensure_ascii=False)
    print(f"\n[APPLY] Snapshot: {snapshot_path} ({len(snapshot_docs)} documentos)")

    # ── Preparar: excluir reasign del pool, asignarlas primero ─────────
    reasign_ids_set = set(reasign_ids)
    assigned_counter = dict(agent_stock)

    # 1a. Asignar primero las reasignaciones (directo a destino)
    reasign_applied = 0
    reasign_skipped = 0
    for prop, from_aid, to_aid, slug in reasign_plan:
        to_agent = agents[to_aid]
        assigned_counter[to_aid] += 1
        # Guarda atomica: verificar que sigue sin gestion y sin agente
        fresh = coll.find_one({"_id": prop["_id"]})
        if fresh:
            has_ev, _ = has_management_evidence(fresh, events_coll)
            if has_ev:
                reasign_skipped += 1
                logger.warning(f"Reasign {prop['_id']}: gestion detectada, omitida")
                continue
        result = coll.update_one(
            {"_id": prop["_id"],
             "$or": [{"gestion.ejecutivo_id": {"$exists": False}}, {"gestion.ejecutivo_id": None}]},
            {"$set": {
                "gestion.ejecutivo_id": to_agent["id"],
                "gestion.ejecutivo_asignado": to_agent["name"],
                "gestion.ejecutivo_nombre": to_agent["name"],
                "gestion.ejecutivo_email": to_agent["email"],
                "gestion.fecha_asignacion": now,
                "gestion.estado": "NUEVO",
                "gestion.asignacion_version": "v5_stock_rebalancing",
                "gestion.asignacion_comuna_slug": slug,
                "gestion.classification_at_assignment": prop.get("classification", {}).get("state"),
                "gestion.assignment_cycle_id": None,
                "gestion.first_valid_action_at": None,
                "gestion.reassignment_reason": "stock_rebalancing",
                "gestion.reassigned_at": now,
            },
             "$push": {"gestion.historial_asignaciones": {
                "ejecutivo_id": to_agent["id"],
                "ejecutivo_nombre": to_agent["name"],
                "comuna_slug": slug,
                "assigned_at": now,
                "assignment_version": "v5_stock_rebalancing",
                "reason": "stock_rebalancing",
                "nota": "Reasignada por balance de stock pendiente. Sin evidencia de gestion.",
            }}}
        )
        if result.modified_count > 0:
            reasign_applied += 1
        else:
            reasign_skipped += 1

    print(f"[APPLY] Reasignaciones (balance): {reasign_applied} aplicadas, {reasign_skipped} omitidas")

    # 1b. Asignar el resto del pool (excluyendo las ya reasignadas)
    pool_applied = 0
    pool_skipped = 0
    for p in pool:
        if p["_id"] in reasign_ids_set:
            continue
        slug = norm(p.get("comuna_slug") or p.get("comuna"))
        candidates = comuna_to_agents.get(slug, [])
        if not candidates:
            pool_skipped += 1
            continue
        # Guarda atomica: verificar sin gestion
        fresh = coll.find_one({"_id": p["_id"]})
        if fresh:
            has_ev, _ = has_management_evidence(fresh, events_coll)
            if has_ev:
                pool_skipped += 1
                continue
            # Verificar que sigue sin ejecutivo
            if (fresh.get("gestion") or {}).get("ejecutivo_id"):
                pool_skipped += 1
                continue
        best = min(candidates, key=lambda aid: assigned_counter.get(aid, 0))
        assigned_counter[best] += 1
        winner = agents[best]
        result = coll.update_one(
            {"_id": p["_id"],
             "$or": [{"gestion.ejecutivo_id": {"$exists": False}}, {"gestion.ejecutivo_id": None}]},
            {"$set": {
                "gestion.ejecutivo_id": winner["id"],
                "gestion.ejecutivo_asignado": winner["name"],
                "gestion.ejecutivo_nombre": winner["name"],
                "gestion.ejecutivo_email": winner["email"],
                "gestion.fecha_asignacion": now,
                "gestion.estado": "NUEVO",
                "gestion.asignacion_version": "v5_stock_redistribution",
                "gestion.asignacion_comuna_slug": slug,
                "gestion.classification_at_assignment": p.get("classification", {}).get("state"),
                "gestion.assignment_cycle_id": None,
                "gestion.first_valid_action_at": None,
            },
             "$push": {"gestion.historial_asignaciones": {
                "ejecutivo_id": winner["id"],
                "ejecutivo_nombre": winner["name"],
                "comuna_slug": slug,
                "classification_state": p.get("classification", {}).get("state"),
                "assigned_at": now,
                "assignment_version": "v5_stock_redistribution",
                "reason": "stock_redistribution",
            }}}
        )
        if result.modified_count > 0:
            pool_applied += 1
        else:
            pool_skipped += 1

    print(f"[APPLY] Pool: {pool_applied} asignadas, {pool_skipped} omitidas")

    # 3. Verificar stock pendiente final
    print(f"\n[APPLY] Verificacion de stock pendiente final:")
    for aid in sorted(agent_stock, key=lambda x: agent_stock[x]):
        a = agents[aid]
        real_stock = stock_pendiente(aid, coll, events_coll)
        print(f"  {a['name']}: stock_pendiente={real_stock}")

    # 4. Elegibles sin agente
    remaining = 0
    for p in coll.find({"$or": [{"gestion.ejecutivo_id": {"$exists": False}}, {"gestion.ejecutivo_id": None}]}):
        if is_eligible(p):
            has_ev, _ = has_management_evidence(p, events_coll)
            if not has_ev:
                remaining += 1
    print(f"\n  Elegibles sin agente (post-redist): {remaining}")

    # 5. Verificar gestionadas intactas
    managed_intact = True
    for aid, mprops in agent_managed.items():
        for mp in mprops:
            doc = coll.find_one({"_id": mp["_id"]}, {"gestion": 1})
            if doc:
                v = doc.get("gestion", {}).get("asignacion_version", "")
                if v in ("v5_stock_redistribution", "v5_stock_rebalancing"):
                    managed_intact = False
                    logger.error(f"Gestionada modificada: {mp['_id']}")
    print(f"  Gestionadas intactas: {managed_intact}")

    print(f"\n[APPLY] Completado.")
    print(f"  Pool asignadas:    {pool_applied}")
    print(f"  Reasignadas:       {reasign_applied}")
    print(f"  Total exitosas:    {pool_applied + reasign_applied}")
    print(f"  Omitidas:          {pool_skipped + reasign_skipped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    dry_run = args.dry_run or not args.apply
    run(dry_run=dry_run)
