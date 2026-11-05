"""
Orquestador territorial de scraping.
Ejecuta Yapo y Toctoc solo en comunas de agentes activos.
No modifica los scrapers existentes: los invoca via subprocess con sus CLI actuales.

Uso:
    python scraper/run_territorial.py --dry-run --portal yapo --commune nunoa --max-pages 3
    python scraper/run_territorial.py --dry-run --portal all                   # todas las comunas de agentes
    python scraper/run_territorial.py --portal toctoc --exclude-commune talca
    python scraper/run_territorial.py --portal all --max-pages 5 --max-new-properties 50 --max-deepseek-calls 10
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Agregar raiz del proyecto al path para importar config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import Config
from chatbot.storage import get_db


def norm_commune(v: str) -> str:
    """Normaliza comuna a slug canonico (mismo que normalize_commune_canonical)."""
    if not v:
        return ""
    s = str(v).lower().strip()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.replace("ñ", "n")
    s = re.sub(r"[^a-z0-9\s_-]", "", s)
    s = re.sub(r"[\s_]+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def get_active_agent_communes(db) -> dict[str, list[str]]:
    """Retorna {comuna_norm: [nombre_agente, ...]} para agentes activos con comunas configuradas."""
    agents = db["usuarios"].find(
        {"is_active": True, "rol": "agente",
         "comunas_interes_norm": {"$exists": True, "$ne": [], "$type": "array"}},
        {"nombre": 1, "comunas_interes_norm": 1}
    )
    result = defaultdict(list)
    for a in agents:
        name = a.get("nombre", "?")
        for cn in a.get("comunas_interes_norm", []):
            cn = norm_commune(cn)
            if cn:
                result[cn].append(name)
    return dict(result)


class TerritorialOrchestrator:
    def __init__(self, portal: str, communes: list[str] | None = None,
                 exclude: list[str] | None = None,
                 max_pages: int = 3, max_new: int = 50, max_deepseek: int = 0,
                 dry_run: bool = True, report_path: str | None = None):
        self.portal = portal  # "yapo", "toctoc", "all"
        self.allowed = set(norm_commune(c) for c in (communes or []) if c)
        self.excluded = set(norm_commune(c) for c in (exclude or []) if c)
        self.max_pages = max_pages
        self.max_new = max_new
        self.max_deepseek = max_deepseek
        self.dry_run = dry_run
        self.report_path = report_path

        self.db = get_db()
        self.results = []  # lista de dicts con metricas por ejecucion

    def resolve_communes(self) -> list[str]:
        """Resuelve la lista final de comunas: allowlist > agentes activos - exclusiones."""
        if self.allowed:
            communes = sorted(self.allowed - self.excluded)
        else:
            agent_communes = get_active_agent_communes(self.db)
            communes = sorted(set(agent_communes.keys()) - self.excluded)
        return communes

    def run_yapo(self, commune: str) -> dict:
        """Invoca el scraper Yapo para una comuna."""
        scraper_dir = Path(__file__).resolve().parent
        script = scraper_dir / "run_owner_hunt.py"

        cmd = [
            sys.executable, str(script),
            "run-full",
            "--target-commune", commune,
            "--max-pages", str(self.max_pages),
            "--max-urls", str(self.max_new * 3),
            "--no-llm",
        ]
        if self.dry_run:
            cmd.append("--dry-run")

        result = {
            "portal": "yapo",
            "commune": commune,
            "pages": self.max_pages,
            "dry_run": self.dry_run,
            "urls_discovered": 0,
            "already_existing": 0,
            "new_candidates": 0,
            "downloaded": 0,
            "deepseek_calls": 0,
            "mongodb_writes": 0,
            "errors": 0,
            "duration_s": 0,
            "stdout_tail": "",
        }

        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd, cwd=str(scraper_dir), capture_output=True, text=True, timeout=600,
            )
            result["duration_s"] = round(time.perf_counter() - t0, 1)
            result["stdout_tail"] = proc.stdout[-500:] if proc.stdout else ""
            if proc.returncode != 0:
                result["errors"] = 1
                result["stderr"] = proc.stderr[-300:] if proc.stderr else "unknown"
        except subprocess.TimeoutExpired:
            result["duration_s"] = round(time.perf_counter() - t0, 1)
            result["errors"] = 1
            result["stderr"] = "timeout after 600s"
        except Exception as e:
            result["duration_s"] = round(time.perf_counter() - t0, 1)
            result["errors"] = 1
            result["stderr"] = str(e)[:300]

        self.results.append(result)
        return result

    def run_toctoc(self, commune: str) -> dict:
        """Invoca el scraper Toctoc para una comuna."""
        scraper_dir = Path(__file__).resolve().parent.parent / "scraper_toctoc"
        script = scraper_dir / "run_toctoc.py"

        cmd = [
            sys.executable, str(script),
            "discover",
            "--comuna", commune,
            "--max-pages", str(self.max_pages),
            "--max-urls", str(self.max_new * 3),
            "--operacion", "venta",
        ]
        if self.dry_run:
            cmd.append("--dry-run")

        result = {
            "portal": "toctoc",
            "commune": commune,
            "pages": self.max_pages,
            "dry_run": self.dry_run,
            "urls_discovered": 0,
            "already_existing": 0,
            "new_candidates": 0,
            "downloaded": 0,
            "deepseek_calls": 0,
            "mongodb_writes": 0,
            "errors": 0,
            "duration_s": 0,
            "stdout_tail": "",
        }

        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd, cwd=str(scraper_dir), capture_output=True, text=True, timeout=600,
            )
            result["duration_s"] = round(time.perf_counter() - t0, 1)
            result["stdout_tail"] = proc.stdout[-500:] if proc.stdout else ""
            if proc.returncode != 0:
                result["errors"] = 1
                result["stderr"] = proc.stderr[-300:] if proc.stderr else "unknown"
        except subprocess.TimeoutExpired:
            result["duration_s"] = round(time.perf_counter() - t0, 1)
            result["errors"] = 1
            result["stderr"] = "timeout after 600s"
        except Exception as e:
            result["duration_s"] = round(time.perf_counter() - t0, 1)
            result["errors"] = 1
            result["stderr"] = str(e)[:300]

        self.results.append(result)
        return result

    def run(self):
        communes = self.resolve_communes()

        print(f"{'='*70}")
        print(f"  ORQUESTADOR TERRITORIAL {'(DRY-RUN)' if self.dry_run else '(LIVE)'}")
        print(f"{'='*70}")
        print(f"  Portal: {self.portal}")
        print(f"  Comunas: {len(communes)}")
        if len(communes) <= 20:
            for c in communes:
                agents = get_active_agent_communes(self.db).get(c, [])
                print(f"    {c} -> {', '.join(agents)}")
        else:
            print(f"    {', '.join(communes[:10])} ... y {len(communes)-10} mas")
        print(f"  Max paginas por comuna: {self.max_pages}")
        print(f"  Max nuevas por comuna:  {self.max_new}")
        print(f"  Max DeepSeek calls:     {self.max_deepseek}")
        print(f"  Exclusiones:            {sorted(self.excluded) if self.excluded else 'ninguna'}")
        print(f"  Dry-run: {'SI (sin escritura, sin DeepSeek)' if self.dry_run else 'NO (produccion)'}")
        print(f"  Modo: {'SOLO SIMULACION - no se ejecutan scrapers' if self.dry_run else 'EJECUTANDO SCRAPERS'}")

        if self.dry_run:
            print(f"\n  [DRY-RUN] No se ejecutaran los scrapers.")
            print(f"  Se consultaria la BD para ver cuantas propiedades ya existen por comuna.")
            # Mostrar cuantas propiedades ya existen por comuna (conteo rapido)
            coll = self.db[Config.CAPTACION_COLLECTION_NAME]
            for c in communes[:15]:
                cnt = coll.count_documents({"comuna_slug": c})
                print(f"    {c}: {cnt} existentes en BD")
            if len(communes) > 15:
                print(f"    ... y {len(communes)-15} comunas mas")
            # Guardar reporte
            self.save_report()
            return

        # Modo live: ejecutar scrapers comuna por comuna
        for i, commune in enumerate(communes):
            print(f"\n[{i+1}/{len(communes)}] {commune} ...")
            if self.portal in ("yapo", "all"):
                r = self.run_yapo(commune)
                print(f"  Yapo: {r['duration_s']}s, errors={r['errors']}")
            if self.portal in ("toctoc", "all"):
                r = self.run_toctoc(commune)
                print(f"  Toctoc: {r['duration_s']}s, errors={r['errors']}")

        self.save_report()
        self.print_summary()

    def save_report(self):
        path = self.report_path or f"reports/territorial_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "portal": self.portal,
            "dry_run": self.dry_run,
            "communes_total": len(self.resolve_communes()),
            "results": self.results,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, default=str, indent=2, ensure_ascii=False)
        print(f"\n  Reporte: {path}")

    def print_summary(self):
        if not self.results:
            return
        print(f"\n{'='*70}")
        print(f"  RESUMEN")
        print(f"{'='*70}")
        print(f"  {'Portal':<8} {'Comuna':<22} {'Duracion':>8} {'Errores':>7}")
        for r in self.results:
            print(f"  {r['portal']:<8} {r['commune']:<22} {r['duration_s']:>7.1f}s {r['errors']:>7}")


def main():
    parser = argparse.ArgumentParser(description="Orquestador territorial de scraping")
    parser.add_argument("--portal", default="all", choices=["yapo", "toctoc", "all"])
    parser.add_argument("--commune", action="append", default=[],
                        help="Comuna(s) a incluir (allowlist). Repetir para varias.")
    parser.add_argument("--exclude-commune", action="append", default=[],
                        help="Comuna(s) a excluir. Repetir para varias.")
    parser.add_argument("--max-pages", type=int, default=3, help="Max paginas por comuna")
    parser.add_argument("--max-new-properties", type=int, default=50,
                        help="Max propiedades nuevas por comuna")
    parser.add_argument("--max-deepseek-calls", type=int, default=0,
                        help="Max llamadas a DeepSeek (0 = deshabilitado)")
    parser.add_argument("--dry-run", action="store_true", help="Solo diagnostico, sin scraping")
    parser.add_argument("--report-path", default=None, help="Ruta del reporte JSON")
    args = parser.parse_args()

    orch = TerritorialOrchestrator(
        portal=args.portal,
        communes=args.commune if args.commune else None,
        exclude=args.exclude_commune if args.exclude_commune else None,
        max_pages=args.max_pages,
        max_new=args.max_new_properties,
        max_deepseek=args.max_deepseek_calls,
        dry_run=args.dry_run,
        report_path=args.report_path,
    )
    orch.run()


if __name__ == "__main__":
    main()
