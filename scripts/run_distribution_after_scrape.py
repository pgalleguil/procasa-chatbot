"""Dispara la distribución global de captaciones al terminar un scrape.

Se ejecuta como subprocess desde los scrapers locales una vez que el lote ha
persistido nuevos documentos en MongoDB. El distribuidor consulta el pool
global CP/Yapo/Toctoc y usa un lock compartido en MongoDB; un segundo trigger
sale limpiamente si ya existe una corrida activa.

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
        # Keep this launcher compatible with older local scraper shims that
        # monkeypatch the public function without keyword arguments. The
        # distributor itself records the default post-scrape/manual source.
        assigned = distribute_sourced_leads()
        print(f"[DISTRIBUCION] Post-scrape: {assigned} captaciones asignadas.")
        return 0
    except Exception as e:
        print(f"[DISTRIBUCION] Error en distribucion post-scrape: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
