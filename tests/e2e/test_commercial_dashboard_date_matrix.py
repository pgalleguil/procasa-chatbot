"""Production/local Chromium matrix for dashboard date contracts."""
import argparse
import json
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import sync_playwright

from analytics.commercial_periods import comparison_period, preset_range

ANCHOR = date(2026, 7, 21)
PRESETS = ("today", "week", "month", "30d", "custom")
COMPARISONS = ("auto", "prev", "yoy", "none")


def ranges(preset, compare):
    start, end = ((date(2026, 7, 10), date(2026, 7, 15)) if preset == "custom"
                  else preset_range(preset, ANCHOR))
    cs, ce, _ = comparison_period(start, end, compare, preset)
    return start, end, cs, ce


def run(base_url, output):
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, timezone_id="America/Santiago")
        context.add_init_script("""
          (() => { const NativeDate=Date, fixed=new NativeDate('2026-07-21T12:00:00-04:00').valueOf();
            class FrozenDate extends NativeDate { constructor(...a){super(...(a.length?a:[fixed]));} static now(){return fixed;} }
            FrozenDate.parse=NativeDate.parse; FrozenDate.UTC=NativeDate.UTC; window.Date=FrozenDate; })();
        """)
        page = context.new_page()
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        for preset in PRESETS:
            for compare in COMPARISONS:
                start, end, cs, ce = ranges(preset, compare)
                query = urlencode({"period_start": start.isoformat(), "period_end": end.isoformat(),
                                   "compare": compare, "period_preset": preset})
                url = f"{base_url}?{query}"
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_function("document.querySelector('#kpiRow')?.getAttribute('aria-busy') === 'false'", timeout=60000)
                state = page.evaluate("""async () => { const payload=await fetch('/api/analytics/commercial-dashboard'+location.search).then(r=>r.json()); return ({
                  url: location.href, period: document.querySelector('#perCur')?.textContent.trim(),
                  comparison: document.querySelector('#perCmp')?.textContent.trim(),
                  preset: document.querySelector('#presetSeg button.active')?.dataset.p,
                  compare: document.querySelector('#pCompare')?.value,
                  meta: payload?.meta?.period, kpi: document.querySelector('[data-val]')?.textContent.trim(),
                  busy: document.querySelector('#kpiRow')?.getAttribute('aria-busy')
                }); }""")
                meta = state.get("meta") or {}
                previous = meta.get("previous") or {}
                received = (previous.get("start") or None, previous.get("end") or None)
                expected = (cs.isoformat() if cs else None, ce.isoformat() if ce else None)
                checks = [state["preset"] == preset, state["compare"] == compare,
                          (meta.get("current") or {}).get("start") == start.isoformat(),
                          (meta.get("current") or {}).get("end") == end.isoformat(),
                          received == expected, state["busy"] == "false",
                          ("vs." not in state["comparison"].lower()) if compare == "none" else True]
                rows.append({
                    "preset": preset, "compare": compare, "anchor": ANCHOR.isoformat(),
                    "actual_start": start.isoformat(), "actual_end": end.isoformat(),
                    "expected_compare_start": expected[0], "expected_compare_end": expected[1],
                    "received_compare_start": received[0], "received_compare_end": received[1],
                    "visible_period": state["period"], "visible_comparison": state["comparison"],
                    "kpi_received": state["kpi"], "url": state["url"],
                    "status": "PASS" if all(checks) else "FAIL", "checks": checks,
                })
        # URL history contract: two state changes, then backward/forward restore exact URLs.
        page.goto(f"{base_url}?period_start=2026-07-21&period_end=2026-07-21&compare=prev&period_preset=today")
        page.wait_for_function("document.querySelector('#kpiRow')?.getAttribute('aria-busy') === 'false'")
        first = page.url
        page.select_option("#pCompare", "none")
        page.wait_for_function("new URL(location.href).searchParams.get('compare') === 'none'")
        page.wait_for_function("document.querySelector('#kpiRow')?.getAttribute('aria-busy') === 'false'")
        second = page.url
        page.go_back(wait_until="domcontentloaded"); page.wait_for_function("document.querySelector('#kpiRow')?.getAttribute('aria-busy') === 'false'"); back = page.url
        page.go_forward(wait_until="domcontentloaded"); page.wait_for_function("document.querySelector('#kpiRow')?.getAttribute('aria-busy') === 'false'"); forward = page.url
        browser.close()
    output.mkdir(parents=True, exist_ok=True)
    result = {"anchor": "2026-07-21T12:00:00-04:00", "timezone": "America/Santiago",
              "presets": list(PRESETS), "comparisons": list(COMPARISONS), "rows": rows,
              "contracts": {
                  "week": "lunes hasta anchor; prev desplaza 7 días",
                  "month": "día 1 hasta anchor; mes previo con igual duración, ajustado hacia atrás si el mes es más corto",
                  "year_over_year": "mismas fechas calendario un año antes; 29 feb se ajusta a 28 feb",
                  "bounds": "[medianoche local inicial, primer instante válido del día siguiente) convertido a UTC",
              },
              "history": {"first": first, "second": second, "back": back, "forward": forward,
                          "status": "PASS" if back == first and forward == second else "FAIL"},
              "console_errors": console_errors}
    (output / "date-comparison-matrix.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(row["status"] == "PASS" for row in rows)
    lines = ["# Matriz temporal del Dashboard Comercial", "", f"- Ancla: `{result['anchor']}`",
             "- Zona horaria: `America/Santiago`", f"- Resultado: **{passed}/{len(rows)} PASS**", "",
             "## Contratos", "", "- Semana: lunes hasta la fecha ancla; `prev` desplaza siete días.",
             "- Mes: día 1 hasta la fecha ancla; conserva duración en el mes previo y, si este es más corto, ajusta el inicio hacia atrás.",
             "- Año anterior: mismas fechas calendario; 29 de febrero se ajusta al 28 de febrero.",
             "- MongoDB: intervalo semiabierto desde el inicio civil local hasta el primer instante válido del día siguiente, convertido a UTC.", "",
             "| Preset | Comparación | Actual | Comparable esperado | Comparable recibido | Estado |", "|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['preset']} | {row['compare']} | {row['actual_start']} → {row['actual_end']} | {row['expected_compare_start'] or '—'} → {row['expected_compare_end'] or '—'} | {row['received_compare_start'] or '—'} → {row['received_compare_end'] or '—'} | {row['status']} |")
    lines += ["", f"Historial atrás/adelante: **{result['history']['status']}**",
              f"Errores de consola: **{len(console_errors)}**"]
    (output / "date-comparison-matrix.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if passed != len(rows) or result["history"]["status"] != "PASS" or console_errors:
        raise SystemExit(1)
    print(f"{passed}/{len(rows)} PASS; history PASS; console 0")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://procasa-chatbot-yr8d.onrender.com/analytics/commercial")
    parser.add_argument("--output", default="reports/commercial-dashboard")
    args = parser.parse_args()
    run(args.base_url, Path(args.output))
