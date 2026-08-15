"""Ejecuta Toctoc para las comunas de preferencia del equipo.

Canario recomendado:
    python scripts/run_toctoc_team_preferences.py --apply \
      --communes santiago-centro,nunoa,maipu --max-urls 5

Lote completo:
    python scripts/run_toctoc_team_preferences.py --apply --max-urls 50

Sin ``--apply`` solo muestra las combinaciones que ejecutaría. El scraper
Toctoc conserva su deduplicación, HTML backup, proxies y clasificación.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOCTOC_RUNNER = ROOT / "scraper_toctoc" / "run_toctoc.py"


def load_team_communes() -> list[str]:
    sys.path.insert(0, str(ROOT))
    from chatbot.storage import get_db

    rows = get_db()["usuarios"].find(
        {"is_active": True, "rol": "agente", "comunas_interes_norm": {"$exists": True, "$ne": []}},
        {"comunas_interes_norm": 1},
    )
    return sorted({str(c).strip() for row in rows for c in (row.get("comunas_interes_norm") or []) if str(c).strip()})


def main() -> int:
    parser = argparse.ArgumentParser(description="Scraper Toctoc por preferencias del equipo")
    parser.add_argument("--apply", action="store_true", help="Escribe documentos nuevos/actualizados en MongoDB")
    parser.add_argument("--communes", default="", help="Comunas separadas por coma; vacío = todas las preferencias")
    parser.add_argument("--operations", default="venta,arriendo", help="Operaciones separadas por coma")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="Límite artificial; vacío = todas las páginas")
    parser.add_argument("--max-urls", type=int, default=None,
                        help="Límite artificial; vacío = todas las URLs")
    parser.add_argument("--limit", type=int, default=None,
                        help="Límite de procesamiento; vacío = todos los descubiertos")
    parser.add_argument("--estado", type=int, choices=[0, 1, 2], default=None,
                        help="0=todos, 1=nuevo, 2=usado; vacío = todos")
    parser.add_argument("--publicador", type=int, choices=[0, 1, 2], default=None,
                        help="0=todos, 1=profesional, 2=particular; vacío = todos")
    parser.add_argument("--proxy-mode", choices=["direct", "proxy", "auto"], default="auto")
    parser.add_argument("--use-playwright-discovery", action="store_true")
    parser.add_argument("--no-llm", action="store_true", help="No usar clasificación DeepSeek")
    args = parser.parse_args()

    communes = [c.strip() for c in args.communes.split(",") if c.strip()] if args.communes else load_team_communes()
    operations = [o.strip().lower() for o in args.operations.split(",") if o.strip()]
    invalid = sorted(set(operations) - {"venta", "arriendo"})
    if invalid:
        parser.error(f"operaciones inválidas: {invalid}")

    batch_id = datetime.now(timezone.utc).strftime("toctoc_team_%Y%m%d_%H%M%S")
    commands = []
    for comuna in communes:
        for operation in operations:
            cmd = [
                sys.executable, str(TOCTOC_RUNNER), "run-full",
                "--operacion", operation,
                "--comuna", comuna,
                "--proxy-mode", args.proxy_mode,
                "--use-playwright",
                "--disable-post-distribution",
                "--write-db" if args.apply else "--dry-run",
            ]
            if args.max_pages is not None:
                cmd.extend(["--max-pages", str(args.max_pages)])
            if args.max_urls is not None:
                cmd.extend(["--max-urls", str(args.max_urls)])
            if args.limit is not None:
                cmd.extend(["--limit", str(args.limit)])
            if args.estado is not None:
                cmd.extend(["--estado", str(args.estado)])
            if args.publicador is not None:
                cmd.extend(["--publicador", str(args.publicador)])
            if args.use_playwright_discovery:
                cmd.append("--use-playwright-discovery")
            if args.no_llm:
                cmd.append("--no-llm")
            commands.append((comuna, operation, cmd))

    report = {
        "batch_id": batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "apply": args.apply,
        "communes": communes,
        "operations": operations,
        "combinations": len(commands),
        "runs": [],
    }
    report_dir = ROOT / "reports" / "toctoc_team"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{batch_id}.json"

    print(f"Comunas: {len(communes)} | Operaciones: {operations} | Combinaciones: {len(commands)}")
    print(f"Modo: {'APPLY' if args.apply else 'DRY-RUN'} | max_urls por combinación: {args.max_urls}")
    for comuna, operation, cmd in commands:
        print(f"\n=== Toctoc {operation} / {comuna} ===")
        if not args.apply:
            print("DRY-RUN:", " ".join(cmd))
            report["runs"].append({"comuna": comuna, "operacion": operation, "status": "not_run"})
            continue
        completed = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
        output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        print(output[-4000:])
        report["runs"].append({
            "comuna": comuna,
            "operacion": operation,
            "returncode": completed.returncode,
            "status": "ok" if completed.returncode == 0 else "error",
            "output_tail": output[-4000:],
        })
        if completed.returncode != 0:
            print(f"[WARN] Falló {operation}/{comuna}; se continúa con la siguiente combinación.")

    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReporte del lote: {report_path}")
    return 0 if all(r.get("status") in {"ok", "not_run"} for r in report["runs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
