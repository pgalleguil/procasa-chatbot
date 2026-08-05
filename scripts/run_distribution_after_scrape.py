"""Dispara la distribucion de captaciones nuevas al terminar un scrape.

Se ejecuta como subprocess desde los scripts de scraping (run_toctoc.py,
run_territorial_expansion.py, run_toctoc_incremental.py) una vez que el lote
ha persistido nuevos documentos en MongoDB. Remplaza el antiguo loop horario
del servidor: no tiene sentido asignar cada hora si no hubo scraping nuevo.

Uso (desde un scraper, al final del lote):
    subprocess.run([sys.executable, "scripts/run_distribution_after_scrape.py"])
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main():
    try:
        from api_captacion import distribute_sourced_leads
    except Exception as e:
        print(f"[DISTRIBUCION] No se pudo importar distribute_sourced_leads: {e}", file=sys.stderr)
        return 1
    try:
        assigned = distribute_sourced_leads()
        print(f"[DISTRIBUCION] Post-scrape: {assigned} captaciones asignadas.")
        return 0
    except Exception as e:
        print(f"[DISTRIBUCION] Error en distribucion post-scrape: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
