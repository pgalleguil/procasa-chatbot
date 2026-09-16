"""Zero-I/O, reproducible stateful Phase 3 evaluation runner.

It deliberately uses a fake catalogue and writer so the non-LLM gates can be
tested without credentials, provider calls or MongoDB.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from chatbot import phase3_conversation as policy
from chatbot.phase3_eval_harness import StatefulReplay, fake_search

CATALOGUE = [
    {"id":"N-R2", "operation":"Arriendo", "comuna":"Ñuñoa", "tipo":"Departamento", "dormitorios":2, "banos":2, "estacionamientos":1, "precio_clp":700000, "gastos_comunes":90000},
    {"id":"P-R2", "operation":"Arriendo", "comuna":"Providencia", "tipo":"Departamento", "dormitorios":2, "banos":2, "estacionamientos":1, "precio_clp":800000},
    {"id":"N-V3", "operation":"Venta", "comuna":"Ñuñoa", "tipo":"Casa", "dormitorios":3, "banos":2, "estacionamientos":2, "precio_uf":5200},
]

def _turn(state, message, *, facts=None, human=False):
    lead={"human_active":human or state.owner == "human", "conversation_owner":state.owner,
          "prospecto":{"operacion":state.operation}}
    chat=[*state.history,{"role":"user","content":message}]
    phase=policy.build_conversation_state(lead, chat, property_context={"property_id":state.property_id,"operation":state.operation})
    action=policy.select_next_best_action(phase,message,facts=facts or {},property_resolved=bool(state.property_id))
    response=policy.deterministic_response(phase,action,facts or {},message) or ""
    state.history += [{"role":"user","content":message},{"role":"assistant","content":response}]
    if phase.get("visit_intent"): state.capture_visit(phase.get("visit_preference"))
    if action["next_best_action"] == "HANDOFF_HUMAN": state.handoff_human()
    return phase, action, response

def _scenario(index):
    kind=index % 8
    if kind == 0: return ["Busco departamento para arrendar en Ñuñoa", "2 dormitorios", "máximo 700 mil", "ideal cerca del metro", "¿tiene estacionamiento?"], "search"
    if kind == 1: return ["¿Está disponible, cuánto salen los gastos comunes y puedo verla mañana?", "jueves después de las 18", "gracias"], "multi"
    if kind == 2: return ["Tengo un departamento de 54 m2, piso 8, 2 dormitorios", "busco corredora para publicarlo y coordinar visitas", "¿trabajan sin exclusividad?"], "owner"
    if kind == 3: return ["Quiero verla", "mañana tipo 10", "perfecto"], "visit"
    if kind == 4: return ["Busco departamento para arrendar en Ñuñoa", "mejor Providencia", "puedo llegar hasta 800 mil", "¿cuánto mide?"], "search_change"
    if kind == 5: return ["Busco casa en venta", "en realidad busco arrendar", "2 dormitorios", "gracias"], "operation"
    if kind == 6: return ["no me respondiste los gastos comunes", "¿cuánto son?", "👍"], "frustration"
    return ["¿Tiene estacionamiento y bodega? ¿Y cuánto mide?", "Prefiero hablar con una persona", "puedo verla mañana", "gracias"], "handoff"

def execute_conversation(index):
    state=StatefulReplay(); state.apply_reference(property_id="N-R2",operation="Arriendo")
    sequence, kind=_scenario(index); failures=[]; turns=0
    for message in sequence:
        if "Ñuñoa" in message: state.apply_search(operation="Arriendo",comuna="Ñuñoa",tipo="Departamento")
        if "Providencia" in message: state.apply_search(comuna="Providencia")
        if "2 dormitorios" in message: state.apply_search(dormitorios=2)
        if "700 mil" in message: state.apply_search(budget_clp=700000)
        if "800 mil" in message: state.apply_search(budget_clp=800000)
        if "venta" in message.casefold(): state.apply_reference(operation="Venta")
        if "arrendar" in message.casefold(): state.apply_reference(operation="Arriendo")
        found=fake_search(CATALOGUE,state.criteria,allow_relaxation=False)
        prop=found["results"][0] if len(found["results"]) == 1 else next((p for p in CATALOGUE if p["id"] == state.property_id),{})
        phase, action, response=_turn(state,message,facts=prop)
        turns += 1
        if phase["actor_intent"] == "OWNER" and action["next_best_action"] == "PROPOSE_VISIT": failures.append("owner_to_visit")
        if message.strip() in {"👍","gracias","perfecto"} and response: failures.append("ack_response")
        if state.owner == "human" and response: failures.append("human_outbound")
        if kind == "multi" and "gastos comunes" in message and "90000" not in response: failures.append("dropped_common_expenses")
        if kind.startswith("search") and message == sequence[-1] and found["status"] != "exact": failures.append("search_context_lost")
    return {"scenario_id":f"fake-{index:03d}","kind":kind,"turns":turns,"failures":failures}

def run(count=240, concurrency=1):
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        rows=list(pool.map(execute_conversation,range(count)))
    failures=[{"scenario_id":r["scenario_id"],"cause":cause} for r in rows for cause in r["failures"]]
    return {"total_conversations_executed":len(rows),"total_turns_executed":sum(r["turns"] for r in rows),
            "failures":failures,"failure_count":len(failures),"real_deepseek_calls":0,"real_whatsapp_sends":0,
            "production_mongo_writes":0,"rows":rows}

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--conversations",type=int,default=240); parser.add_argument("--concurrency",type=int,default=1); parser.add_argument("--output",type=Path,default=Path("reports/phase3_fake_stateful.json")); args=parser.parse_args()
    report=run(args.conversations,args.concurrency); args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps({key:report[key] for key in report if key != "rows"}))
if __name__ == "__main__": main()
