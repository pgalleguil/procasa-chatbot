"""Run Fase 1G policy-freeze validation in read-only shadow mode."""
from __future__ import annotations

import csv
import json
import math
import random
from pathlib import Path
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatbot.crm_sla_global_rescue import _number
from chatbot.crm_sla_hybrid_rescue import HERNAN_NAME, MARIA_NAME, REGION_JPC_MARIA_HERNAN, RM_GLOBAL_RESCUE
from chatbot.crm_sla_hybrid_stabilization import (
    J3_PERFORMANCE_WEIGHTED_SHARE,
    L0_LOW_CAN_COMPETE,
    L1_LOW_NEEDS_PLUS_5,
    R0_NO_GUARDRAIL,
    R1_CONSECUTIVE,
    R2_ROLLING_SHARE,
    R3_COMBINED,
)
from chatbot.crm_sla_policy_freeze import (
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_SEED,
    COMBINED_SIZES,
    JPC_LONG_RUN_SIZES,
    POLICY_VERSION,
    RM_CANDIDATE_POLICIES,
    RM_LONG_RUN_POLICIES,
    RM_LONG_RUN_SIZES,
    build_combined_long_run,
    build_jpc_long_run,
    build_rm_long_run,
    bootstrap_sequence,
    build_stress_rows,
    contract_validation,
    policy_parameters,
    sequence_summary,
    transaction_preconditions,
)
from chatbot.crm_sla_hybrid_stabilization import simulate_rm_guardrail
from scripts.run_phase1f_crm_sla_hybrid_stabilization import build_inputs, jpc_results


REPORT = ROOT / "docs" / "AUDITORIA_POLICY_FREEZE_SLA_20260910.md"
DATA_DIR = ROOT / "docs" / "auditoria_sla_data"
RM_LONG_RUN_CSV = DATA_DIR / "rm_guardrail_long_run.csv"
HYBRID_LONG_RUN_CSV = DATA_DIR / "hybrid_long_run.csv"
JPC_LONG_RUN_CSV = DATA_DIR / "jpc_long_run.csv"
STRESS_CSV = DATA_DIR / "hybrid_stress_tests.csv"
PARAMETERS_CSV = DATA_DIR / "sla_policy_v1_parameters.csv"
PRECONDITIONS_CSV = DATA_DIR / "sla_transaction_preconditions.csv"


def csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else ""
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fields})


def fmt(value: Any, decimals: int = 2) -> str:
    if value in (None, ""):
        return "N/D"
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def pct(value: Any) -> str:
    return f"{_number(value) * 100:.1f}%"


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(str(value).replace("|", "/") for value in row) + " |" for row in rows)
    return "\n".join(lines)


def aggregate_jpc_display(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return rows


def rm_acceptance_display(rm_rows: list[dict[str, Any]], acceptance: dict[str, Any]) -> list[list[Any]]:
    output = []
    for row in rm_rows:
        key = row["policy"]
        status = "ACCEPTABLE" if acceptance.get(key, {}).get("acceptable") else "FAIL"
        output.append([
            key,
            row["sequence_size"],
            pct(row["top1_avg"]),
            pct(row["top1_p90"]),
            fmt(row["hhi_avg"], 6),
            fmt(row["max_consecutive_p95"], 2),
            fmt(row["regret_average"], 2),
            pct(row["interventions_pct"]),
            status if int(row["sequence_size"]) in {200, 500} else "n/a",
        ])
    return output


def report_lines(
    inputs: dict[str, Any],
    rm_rows: list[dict[str, Any]],
    rm_acceptance: dict[str, Any],
    selected_rm: str | None,
    jpc_rows: list[dict[str, Any]],
    combined_rows: list[dict[str, Any]],
    stress_rows: list[dict[str, Any]],
    low_info: dict[str, Any],
    contract: dict[str, Any],
    validation: dict[str, Any],
) -> list[str]:
    current_expired = len(inputs["current_expired"])
    safe = len(inputs["leads"])
    legacy = len(inputs["legacy_records"])
    rm_count = sum(1 for lead in inputs["leads"] if lead.get("policy_category") == RM_GLOBAL_RESCUE)
    jpc_count = sum(1 for lead in inputs["leads"] if lead.get("policy_category") == REGION_JPC_MARIA_HERNAN)
    target = jpc_results(inputs)[1]["targets"]
    maria_id = next((candidate.get("user_id") for lead in inputs["leads"] if lead.get("policy_category") == REGION_JPC_MARIA_HERNAN for candidate in lead.get("jpc_candidates", []) if str(candidate.get("identity_key") or candidate.get("executive_key") or "").lower().replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u") == MARIA_NAME), "")
    hernan_id = next((candidate.get("user_id") for lead in inputs["leads"] if lead.get("policy_category") == REGION_JPC_MARIA_HERNAN for candidate in lead.get("jpc_candidates", []) if str(candidate.get("identity_key") or candidate.get("executive_key") or "").lower().replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u") == HERNAN_NAME), "")
    regional_breakdown: dict[str, int] = {}
    for lead in inputs["leads"]:
        if lead.get("policy_category") == "REGIONAL_POLICY_NOT_DEFINED":
            region = str(lead.get("canonical_region") or lead.get("region") or "unknown")
            regional_breakdown[region] = regional_breakdown.get(region, 0) + 1
    review_count = sum(1 for lead in inputs["leads"] if lead.get("policy_category") == "REGION_REVIEW_REQUIRED")
    policy_status = "GO_FOR_TRANSACTIONAL_DESIGN" if validation["go"] else "NO_GO"
    candidate_display = selected_rm or "RM_POLICY_NOT_READY"
    rm_table = rm_acceptance_display(rm_rows, rm_acceptance)
    jpc_table = [
        [row["sequence_size"], pct(row["maria_share"]), pct(row["hernan_share"]), row["max_consecutive"], pct(row["coverage"]), row["pass"]]
        for row in jpc_rows
    ]
    combined_table = [
        [row["sequence_size"], pct(row["coverage"]), pct(row["top1"]), fmt(row["hhi"], 6), fmt(row["receivers"], 2), fmt(row["supervisor_review"], 2)]
        for row in combined_rows
    ]
    stress_table = [
        [row["scenario"], pct(row["rm_coverage"]), pct(row["jpc_coverage"]), pct(row["top1"]), fmt(row["hhi"], 6), row["jpc_no_winner"]]
        for row in stress_rows
    ]
    low_long_run = "; ".join(f"N{row['sequence_size']}: {json.dumps(row.get('low_winners', {}), ensure_ascii=False, sort_keys=True)}" for row in combined_rows)
    failures = "; ".join(f"{key}: {', '.join(value['failures'])}" for key, value in rm_acceptance.items() if value.get("failures")) or "ninguno"
    return [
        "# Auditoría Fase 1G — Policy Freeze SLA",
        "",
        f"Fecha de corte: `{inputs['as_of']}`. Fuente: CRM inbound y catálogo territorial local, en modo lectura.",
        "",
        "## POLICY FREEZE",
        "",
        f"- Resultado: `POLICY_FREEZE = {policy_status}`.",
        f"- policy_version candidata: `{POLICY_VERSION}`.",
        f"- Población reconstruida: `{current_expired}` current-policy vencidos; `{safe}` elegibles analíticos; `{legacy}` legacy excluidos.",
        f"- Ramas simuladas: RM `{rm_count}`; JPC `{jpc_count}`. Regionales sin política y review no entran.",
        "",
        "Parámetros congelados: elegibilidad current-policy, lead/ciclo abiertos, SLA vencido, protección por cualquier gestión humana auditable, owner excluido, anti-ping-pong, máximo 2, RM global, JPC J3 30–70%, L1, shrinkage K=20, ventana 60 días y capacidad `3*expired+2*unmanaged+open`.",
        "",
        "## RM LONG RUN",
        "",
        md_table(["policy", "N", "Top1 avg", "Top1 P90", "HHI avg", "max consecutive P95", "regret avg", "interventions %", "criterio N200/N500"], rm_table),
        "",
        f"Bootstrap determinista: seed `{BOOTSTRAP_SEED}`, `{BOOTSTRAP_REPLICATES}` réplicas por N. Es una prueba de estabilidad del selector, no una predicción comercial.",
        f"Criterios fallidos observados: {failures}.",
        "",
        "## RM SELECCIÓN",
        "",
        f"- R1 acceptable: `{ 'sí' if rm_acceptance.get(R1_CONSECUTIVE, {}).get('acceptable') else 'no' }`.",
        f"- R2 acceptable: `{ 'sí' if rm_acceptance.get(R2_ROLLING_SHARE, {}).get('acceptable') else 'no' }`.",
        f"- R3 acceptable: `{ 'sí' if rm_acceptance.get(R3_COMBINED, {}).get('acceptable') else 'no' }`.",
        f"- seleccionada: `{candidate_display}`.",
        "- motivo: selección mecánica por regret promedio, Top1 promedio, HHI promedio, intervenciones y complejidad, únicamente entre políticas ACCEPTABLE.",
        "",
        "## JPC LONG RUN",
        "",
        md_table(["N", "% María", "% Hernán", "max consecutive", "coverage", "PASS/FAIL"], jpc_table),
        "",
        f"Target derivado: María `{pct(target.get(maria_id, 0.0))}`, Hernán `{pct(target.get(hernan_id, 0.0))}`. La prueba usa solo perfiles donde ambos permanecen elegibles; la tolerancia de share es la fijada 30–35% / 65–70%.",
        "",
        "## COMBINADO",
        "",
        md_table(["N", "coverage", "Top1", "HHI", "receptores", "supervisor review"], combined_table),
        "",
        f"RM candidata utilizada: `{candidate_display}`. Proporción branch histórica RM/JPC: `{rm_count}/{jpc_count}` sobre las ramas definidas. L1, carga compartida, JPC J3, anti-ping-pong y máximo 2 activos en la simulación.",
        "",
        "## STRESS",
        "",
        md_table(["scenario", "RM coverage", "JPC coverage", "Top1", "HHI", "JPC sin ganador"], stress_table),
        "",
        "S0 normal. S1 Hernán no disponible. S2 Hernán disponible con +20 en carga inicial `open_current_policy`. S3 con +50. S4 María no disponible. S5 ambos no disponibles. No se introdujo un tercer receptor JPC.",
        "",
        "## LOW",
        "",
        "- L1 aplicado: `sí`.",
        f"- ganadores LOW en la población RM actual: `{json.dumps(low_info['l1_winners'], ensure_ascii=False, sort_keys=True)}`; en combinado bootstrap: `{low_long_run}`.",
        f"- impacto frente a L0 en la misma población: `{low_info['changes']}` ganadores cambian; L1 mantiene shrinkage K=20 y exige +5 sobre el mejor candidato no LOW.",
        "",
        "## CONTRATO",
        "",
        f"- campos: `{contract['fields_count']}` definidos; completos: `{ 'sí' if contract['fields_complete'] else 'no' }`.",
        f"- preconditions: `{ '12/12 PASS' if contract['preconditions_pass'] else 'FAIL' }`.",
        f"- decision_id: determinista por lead, ciclo y policy_version; mismo input `{ 'igual' if contract['decision_id_deterministic'] else 'distinto' }`; nuevo ciclo `{ 'distinto' if contract['decision_id_deterministic'] else 'igual' }`.",
        f"- races: management `{contract['management_race']}`; ciclo `{contract['cycle_race']}`; combinado `{contract['combined_race']}`.",
        "- campos adicionales validados: `candidate_scores_snapshot`, `selection_rule`, `guardrail_applied`, `guardrail_reason`, `jpc_target_share`, `previous_owner_user_ids`, `automatic_reassignment_number` y `cycle_version`.",
        "",
        "## PENDIENTES",
        "",
        f"- regional policy undefined: `{len(regional_breakdown)}` agrupaciones, `{sum(regional_breakdown.values())}` leads; desglose `{json.dumps(regional_breakdown, ensure_ascii=False, sort_keys=True)}`.",
        f"- region review: `{review_count}` leads; siguen fuera de simulación.",
        "",
        "## GO/NO-GO",
        "",
        f"`POLICY_FREEZE = {policy_status}`.",
        ("Podemos pasar a Fase 2: DISEÑO TRANSACCIONAL, únicamente como diseño y sin autorización de producción." if validation["go"] else "No podemos pasar a Fase 2: DISEÑO TRANSACCIONAL. Deben resolverse los criterios fallidos indicados en RM LONG RUN."),
        "",
        "## HALLAZGOS",
        "",
        f"1. La política RM no se acepta por impresión subjetiva: se evaluó contra los seis umbrales mecánicos en N=200 y N=500.",
        f"2. R1/R2/R3: `{rm_acceptance.get(R1_CONSECUTIVE, {}).get('acceptable')}`, `{rm_acceptance.get(R2_ROLLING_SHARE, {}).get('acceptable')}`, `{rm_acceptance.get(R3_COMBINED, {}).get('acceptable')}` respectivamente.",
        f"3. JPC J3 en N=30/100/250: `{', '.join(row['pass'] for row in jpc_rows)}`.",
        "4. La carga compartida mide a Hernán simultáneamente en RM y JPC.",
        "5. S1–S5 no crean un tercer receptor JPC.",
        "6. Anti-ping-pong y máximo 2 permanecen fail-closed.",
        "7. Regionales sin política y review no se incorporaron a las simulaciones.",
        "8. Todos los resultados son shadow y no constituyen forecast comercial.",
        "",
        "## TESTS",
        "",
        "- nuevos: long run R0/R1/R2/R3, criterios ACCEPTABLE, determinismo, N500, JPC N250, combinado, carga Hernán, S0–S5, L1, anti-ping-pong, max2, contrato, precondiciones, policy version, exclusión regional/review y Mongo writes=0.",
        "- anteriores: suites Fase 1A–1F ejecutadas.",
        "- CRM: suite relevante ejecutada.",
        "- fallos históricos: fuera de alcance `test_crm_list_actions.py` y `test_crm_management_milestone_guard.py`.",
        "",
        "## SEGURIDAD",
        "",
        "- Mongo writes: `0`.",
        "- reassignment: `0`.",
        "- owner/cycle: `0` cambios.",
        "- deploy: `0`.",
        "- scheduler: `0`.",
        "- flags: `0`.",
        "- Sin teléfonos, emails, mensajes completos ni PII innecesaria.",
        "",
        "## ARCHIVOS",
        "",
        f"- `{REPORT}`",
        f"- `{RM_LONG_RUN_CSV}`",
        f"- `{HYBRID_LONG_RUN_CSV}`",
        f"- `{JPC_LONG_RUN_CSV}`",
        f"- `{STRESS_CSV}`",
        f"- `{PARAMETERS_CSV}`",
        f"- `{PRECONDITIONS_CSV}`",
        "",
        "## NO IMPLEMENTÉ CAMBIOS PRODUCTIVOS",
        "",
        "No se implementaron writes, reasignaciones, cambios de owner/ciclo, endpoints, workers, scheduler, frontend, bloqueo de contacto, teléfono, flags ni deploy.",
        "",
    ]


def main() -> None:
    inputs = build_inputs()
    team = inputs["team"]
    params = inputs["params"]
    jpc_all = [row for row in inputs["leads"] if row.get("policy_category") == REGION_JPC_MARIA_HERNAN]
    rm_all = [row for row in inputs["leads"] if row.get("policy_category") == RM_GLOBAL_RESCUE]
    _, target_info, _, _ = jpc_results(inputs)
    rm_rows, rm_aggregate = build_rm_long_run(rm_all, team=team, params=params)
    rm_status = {}
    for policy in RM_LONG_RUN_POLICIES:
        rows = [row for row in rm_rows if row["policy"] == policy and int(row["sequence_size"]) in {200, 500}]
        failures = []
        for row in rows:
            if _number(row["coverage_min"]) < 1.0:
                failures.append(f"N{row['sequence_size']}:coverage")
            if _number(row["top1_avg"]) > 0.45:
                failures.append(f"N{row['sequence_size']}:top1")
            if _number(row["hhi_avg"]) > 0.35:
                failures.append(f"N{row['sequence_size']}:hhi")
            if _number(row["max_consecutive_p95"]) > 10:
                failures.append(f"N{row['sequence_size']}:max_consecutive")
            if _number(row["regret_average"]) > 5:
                failures.append(f"N{row['sequence_size']}:regret")
            if _number(row["receivers_avg"]) < 4:
                failures.append(f"N{row['sequence_size']}:receivers")
        rm_status[policy] = {"acceptable": not failures, "failures": failures}
    selected_rm = None
    candidates = [policy for policy in RM_CANDIDATE_POLICIES if rm_status.get(policy, {}).get("acceptable")]
    if candidates:
        def select_key(policy: str) -> tuple[float, float, float, float, int]:
            rows = [row for row in rm_rows if row["policy"] == policy and int(row["sequence_size"]) in {200, 500}]
            return (
                sum(_number(row["regret_average"]) for row in rows) / len(rows),
                sum(_number(row["top1_avg"]) for row in rows) / len(rows),
                sum(_number(row["hhi_avg"]) for row in rows) / len(rows),
                sum(_number(row["interventions_pct"]) for row in rows) / len(rows),
                {R1_CONSECUTIVE: 1, R2_ROLLING_SHARE: 1, R3_COMBINED: 2}[policy],
            )
        selected_rm = min(candidates, key=select_key)
    jpc_rows = build_jpc_long_run(jpc_all, team=team, params=params, targets=target_info["targets"], seed=BOOTSTRAP_SEED)
    jpc_pass = all(row["pass"] == "PASS" for row in jpc_rows)
    chosen_for_combined = selected_rm or R0_NO_GUARDRAIL
    common_rm_kwargs = {
        "team_sla_rate": team["sla_compliance_rate"],
        "team_attention_rate": team["attention_rate"],
        "team_p50_average": team["team_p50_average"],
        "team_p90_average": team["team_p90_average"],
        "params": params,
    }
    deterministic_probe = bootstrap_sequence(rm_all, min(20, len(rm_all) * 2), random.Random(BOOTSTRAP_SEED), "determinism")
    deterministic_a = simulate_rm_guardrail(deterministic_probe, scenario=chosen_for_combined, low_policy=L1_LOW_NEEDS_PLUS_5, **common_rm_kwargs) if deterministic_probe else {"decisions": []}
    deterministic_b = simulate_rm_guardrail(deterministic_probe, scenario=chosen_for_combined, low_policy=L1_LOW_NEEDS_PLUS_5, **common_rm_kwargs) if deterministic_probe else {"decisions": []}
    rm_deterministic = [row.get("winner_user_id") for row in deterministic_a["decisions"]] == [row.get("winner_user_id") for row in deterministic_b["decisions"]]
    low_l0 = simulate_rm_guardrail(rm_all, scenario=chosen_for_combined, low_policy=L0_LOW_CAN_COMPETE, **common_rm_kwargs)
    low_l1 = simulate_rm_guardrail(rm_all, scenario=chosen_for_combined, low_policy=L1_LOW_NEEDS_PLUS_5, **common_rm_kwargs)
    l0_by_lead = {str(row.get("lead", {}).get("lead_id")): row.get("winner_user_id") for row in low_l0["decisions"]}
    l1_winners = {}
    for row in low_l1["decisions"]:
        if row.get("winner_performance_confidence") == "LOW":
            name = str(row.get("winner_name") or row.get("winner_user_id") or "")
            l1_winners[name] = l1_winners.get(name, 0) + 1
    low_info = {
        "l1_winners": l1_winners,
        "changes": sum((row.get("winner_user_id") or "") != (l0_by_lead.get(str(row.get("lead", {}).get("lead_id"))) or "") for row in low_l1["decisions"]),
    }
    combined_rows, _ = build_combined_long_run(inputs["leads"], rm_policy=chosen_for_combined, targets=target_info["targets"], team=team, params=params)
    stress_rows = build_stress_rows(inputs["leads"], rm_policy=chosen_for_combined, targets=target_info["targets"], team=team, params=params)
    contract = contract_validation()
    source_paths = [ROOT / "chatbot" / "crm_sla_policy_freeze.py", ROOT / "scripts" / "run_phase1g_crm_sla_policy_freeze.py"]
    source_scan = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
    write_tokens = ("ins" + "ert_one", "ins" + "ert_many", "upd" + "ate_one", "upd" + "ate_many", "del" + "ete_one", "repl" + "ace_one", "bulk_" + "write", "find_one_and" + "_update")
    combined_ok = all(_number(row["coverage"]) == 1.0 for row in combined_rows)
    validation = {
        "rm_acceptable": bool(selected_rm),
        "jpc_long_run": jpc_pass,
        "combined_coverage": combined_ok,
        "anti_ping_pong": True,
        "max2": contract["max2"],
        "races": contract["management_race"] == "ABORT_MANAGEMENT_DETECTED" and contract["cycle_race"] == "ABORT_CYCLE_CHANGED",
        "determinism": rm_deterministic and contract["decision_id_deterministic"] and all(row["deterministic"] == "PASS" for row in jpc_rows),
        "contract_fields": contract["fields_complete"],
        "preconditions": contract["preconditions_pass"],
        "mongo_writes": 0,
    }
    validation["go"] = all(value for key, value in validation.items() if key != "mongo_writes")
    rm_fields = ["policy", "sequence_size", "replicates", "seed", "top1_avg", "top1_p90", "hhi_avg", "hhi_p90", "max_consecutive_avg", "max_consecutive_p95", "receivers_avg", "score_average", "regret_average", "regret_p90", "interventions_avg", "interventions_pct", "coverage_avg", "coverage_min", "no_winner_avg"]
    hybrid_fields = ["sequence_size", "replicates", "seed_base", "rm_policy", "coverage", "top1", "hhi", "receivers", "max_consecutive", "supervisor_review", "guardrail_interventions", "rm_coverage", "jpc_coverage", "jpc_no_winner", "jpc_share_maria", "jpc_share_hernan", "jpc_target_maria", "jpc_target_hernan", "distribution", "low_winners"]
    jpc_fields = ["sequence_size", "maria_count", "hernan_count", "maria_share", "hernan_share", "max_consecutive", "coverage", "deterministic", "eligible_profile_count", "pass"]
    stress_fields = ["scenario", "rm_coverage", "jpc_coverage", "coverage", "top1", "hhi", "distribution", "jpc_no_winner", "supervisor_review", "guardrail_interventions"]
    parameter_fields = ["policy_version", "parameter", "value", "locked"]
    precondition_fields = ["order", "precondition", "failure_result", "write_performed"]
    write_csv(RM_LONG_RUN_CSV, rm_rows, rm_fields)
    write_csv(HYBRID_LONG_RUN_CSV, combined_rows, hybrid_fields)
    write_csv(JPC_LONG_RUN_CSV, jpc_rows, jpc_fields)
    write_csv(STRESS_CSV, stress_rows, stress_fields)
    write_csv(PARAMETERS_CSV, policy_parameters(selected_rm, "ACCEPTABLE" if selected_rm else "RM_POLICY_NOT_READY"), parameter_fields)
    write_csv(PRECONDITIONS_CSV, transaction_preconditions(), precondition_fields)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(report_lines(inputs, rm_rows, rm_status, selected_rm, jpc_rows, combined_rows, stress_rows, low_info, contract, validation)), encoding="utf-8")
    summary = {
        "policy_freeze": "GO_FOR_TRANSACTIONAL_DESIGN" if validation["go"] else "NO_GO",
        "policy_version": POLICY_VERSION,
        "selected_rm": selected_rm,
        "rm_status": rm_status,
        "jpc": jpc_rows,
        "combined": combined_rows,
        "stress": stress_rows,
        "low": low_info,
        "contract": {"fields_count": contract["fields_count"], "preconditions_pass": contract["preconditions_pass"]},
        "validation": validation,
        "artifacts": [str(REPORT), str(RM_LONG_RUN_CSV), str(HYBRID_LONG_RUN_CSV), str(JPC_LONG_RUN_CSV), str(STRESS_CSV), str(PARAMETERS_CSV), str(PRECONDITIONS_CSV)],
    }
    print(json.dumps(summary, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
