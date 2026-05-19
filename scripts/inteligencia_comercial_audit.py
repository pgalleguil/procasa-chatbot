#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QA operacional de propiedades_accionables."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

from pymongo import DESCENDING

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db
from scripts.comercial_normalization import normalize_tipo_propiedad

logger = logging.getLogger("inteligencia_comercial_audit")


def safe_int(v: Any) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def run_audit(top_n: int = 100) -> Dict[str, Any]:
    db = get_db()
    col_uc = db["universo_cartera"]
    col_mc = db["mercado_comunal"]
    col_pa = db["propiedades_accionables"]

    uc_docs = list(col_uc.find({"codigo": {"$exists": True, "$ne": ""}}, {"_id": 0, "codigo": 1, "comuna": 1, "tipo": 1, "precio_uf": 1, "disponible": 1}))
    pa_docs = list(col_pa.find({}, {"_id": 0}))

    mc_pairs = set((str(d.get("comuna") or "").strip().lower(), str(d.get("tipo_propiedad") or "").strip().lower()) for d in col_mc.find({}, {"comuna": 1, "tipo_propiedad": 1}))

    uc_by_code = {str(d.get("codigo")).strip(): d for d in uc_docs if str(d.get("codigo") or "").strip()}

    sin_comuna = [c for c, d in uc_by_code.items() if not str(d.get("comuna") or "").strip()]
    sin_tipo = [c for c, d in uc_by_code.items() if not str(d.get("tipo") or "").strip()]
    sin_precio = [c for c, d in uc_by_code.items() if safe_int(d.get("precio_uf")) <= 0]

    tipo_inconsistente = []
    sin_match_mercado = []

    for c, d in uc_by_code.items():
        comuna = str(d.get("comuna") or "").strip()
        tipo = normalize_tipo_propiedad(str(d.get("tipo") or "").strip())
        if comuna and tipo and (comuna.lower(), tipo.lower()) not in mc_pairs:
            sin_match_mercado.append(c)

        t_raw = str(d.get("tipo") or "").strip()
        if t_raw and tipo == "Desconocido":
            tipo_inconsistente.append(c)

    sin_tasacion = [d.get("codigo_propiedad") for d in pa_docs if (d.get("qa_flags") or {}).get("sin_tasacion_individual")]
    ready = [d.get("codigo_propiedad") for d in pa_docs if d.get("ready_para_campana") is True]

    top = list(
        col_pa.find(
            {},
            {
                "_id": 0,
                "codigo_propiedad": 1,
                "ejecutivo": 1,
                "comuna": 1,
                "tipo_propiedad": 1,
                "score_comercial": 1,
                "riesgo_comercial": 1,
                "campana_recomendada": 1,
                "sobreprecio_pct": 1,
                "fuente_valorizacion": 1,
            },
        ).sort("score_comercial", DESCENDING).limit(top_n)
    )

    return {
        "totales": {
            "universo_cartera": len(uc_by_code),
            "propiedades_accionables": len(pa_docs),
        },
        "sin_match_mercado_comunal": sin_match_mercado,
        "sin_comuna": sin_comuna,
        "sin_tipo": sin_tipo,
        "sin_precio": sin_precio,
        "tipo_inconsistente": tipo_inconsistente,
        "sin_tasacion": sin_tasacion,
        "ready_para_campana": ready,
        "top_prioridad_comercial": top,
    }


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Audit de propiedades_accionables")
    ap.add_argument("--top-n", type=int, default=100)
    ap.add_argument("--save-json", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    report = run_audit(top_n=args.top_n)

    summary = {
        "totales": report["totales"],
        "sin_match_mercado_comunal": len(report["sin_match_mercado_comunal"]),
        "sin_comuna": len(report["sin_comuna"]),
        "sin_tipo": len(report["sin_tipo"]),
        "sin_precio": len(report["sin_precio"]),
        "tipo_inconsistente": len(report["tipo_inconsistente"]),
        "sin_tasacion": len(report["sin_tasacion"]),
        "ready_para_campana": len(report["ready_para_campana"]),
    }
    logger.info("Audit resumen: %s", summary)

    if args.save_json:
        out_path = PROJECT_ROOT / "exports" / "inteligencia_comercial_audit.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Audit guardado en %s", out_path)


if __name__ == "__main__":
    main()
