"""QA E2E y evidencia visual del Dashboard Comercial V4."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = (
    "https://procasa-chatbot-yr8d.onrender.com/analytics/commercial"
    "?period_start=2026-07-21&period_end=2026-07-21&compare=prev&period_preset=today"
)
ADVANCED_PARAMS = {
    "executive", "source", "operation", "property_type", "commune",
    "temperature", "property_code", "assignment", "stage",
}


def wait_ready(page: Page) -> None:
    page.wait_for_function("document.querySelector('#kpiRow')?.getAttribute('aria-busy') === 'false'", timeout=90_000)
    page.wait_for_timeout(750)


def clean_context(browser, theme: str, viewport: tuple[int, int]):
    context = browser.new_context(
        viewport={"width": viewport[0], "height": viewport[1]},
        color_scheme=theme,
        reduced_motion="reduce",
    )
    context.add_init_script(
        f"localStorage.clear();sessionStorage.clear();localStorage.setItem('theme','{theme}')"
    )
    return context


def install_local_template(page: Page, production_origin: str) -> str:
    html = (ROOT / "templates" / "analytics" / "commercial_dashboard.html").read_text(encoding="utf-8")
    local_origin = "http://commercial-v4.local"

    def handler(route, request):
        url = request.url
        if not url.startswith(local_origin):
            route.continue_()
            return
        suffix = url[len(local_origin):]
        if suffix.startswith("/static/logo"):
            route.abort()
        elif suffix.startswith("/api/") or suffix.startswith("/static/"):
            response = page.context.request.get(production_origin + suffix, timeout=90_000)
            route.fulfill(status=response.status, headers=response.headers, body=response.body())
        elif suffix.startswith("/analytics/commercial"):
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        else:
            route.abort()

    page.route("**/*", handler)
    return local_origin


def close_context(page: Page, context, local: bool) -> None:
    if local:
        page.unroute_all(behavior="ignoreErrors")
    context.close()


def state(page: Page) -> dict:
    return page.evaluate("""() => ({
      url: location.href,
      executive: document.querySelector('#fExecPrimary')?.value,
      filterLabel: document.querySelector('#filterButtonLabel')?.textContent.trim(),
      chips: Array.from(document.querySelectorAll('#fltChips .cd-chip')).map(x => x.textContent.trim()),
      leadsStatus: document.querySelector('#metaTxt')?.textContent.trim(),
      leadsKpi: document.querySelector('.cd-kpi-val')?.textContent.trim(),
      comparison: document.querySelector('#perCmp')?.textContent.trim(),
      ariaBusy: document.querySelector('#kpiRow')?.getAttribute('aria-busy'),
      updating: document.querySelectorAll('.cd-updating,.is-loading,.is-updating').length,
      disabledActiveControls: Array.from(document.querySelectorAll('button:disabled,select:disabled,input:disabled')).map(x => x.id),
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      headerAndTabsBottom: Math.round(document.querySelector('#tabNav').getBoundingClientRect().bottom),
      theme: document.documentElement.dataset.theme
    })""")


def assert_p0(browser, url: str, local: bool) -> dict:
    context = clean_context(browser, "light", (390, 844))
    page = context.new_page()
    origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
    destination = url
    if local:
        destination = install_local_template(page, origin) + urlsplit(url).path + "?" + urlsplit(url).query
    page.goto(destination, wait_until="domcontentloaded", timeout=90_000)
    wait_ready(page)
    initial = state(page)
    assert initial["executive"] == "", initial
    assert initial["filterLabel"] == "Filtros", initial
    assert initial["chips"] == [], initial
    assert initial["leadsKpi"] == "5" and "5 leads" in initial["leadsStatus"], initial
    assert "20 jul 2026" in initial["comparison"], initial
    assert not (ADVANCED_PARAMS & set(dict(x.split("=", 1) for x in urlsplit(initial["url"]).query.split("&") if "=" in x))), initial
    assert initial["headerAndTabsBottom"] <= 300, initial

    page.locator("#btnFilters").click()
    page.locator("#fTemp").select_option("HOT")
    page.locator("#btnApply").click()
    wait_ready(page)
    filtered = state(page)
    assert filtered["filterLabel"] == "Filtros · 1", filtered
    assert len(filtered["chips"]) == 1 and "Hot" in filtered["chips"][0], filtered
    assert "temperature=HOT" in filtered["url"], filtered

    page.reload(wait_until="domcontentloaded", timeout=90_000)
    wait_ready(page)
    reloaded = state(page)
    assert reloaded["filterLabel"] == "Filtros · 1" and len(reloaded["chips"]) == 1, reloaded

    page.locator("#btnFilters").click()
    page.locator("#btnReset").click()
    wait_ready(page)
    reset = state(page)
    assert reset["filterLabel"] == "Filtros" and reset["chips"] == [], reset
    assert "temperature=" not in reset["url"], reset
    assert reset["leadsKpi"] == "5" and "5 leads" in reset["leadsStatus"], reset
    close_context(page, context, local)
    return {"initial": initial, "filtered": filtered, "reloaded": reloaded, "reset": reset}


def capture_matrix(browser, url: str, out: Path, local: bool) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    results = []
    sizes = ((1440, 900), (1366, 768), (1024, 768), (768, 1024), (430, 932), (390, 844), (360, 800))
    origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
    for theme in ("light", "dark"):
        for width, height in sizes:
            context = clean_context(browser, theme, (width, height))
            page = context.new_page()
            errors = []
            page.on("console", lambda message, errors=errors: errors.append(message.text) if message.type == "error" else None)
            destination = url
            if local:
                destination = install_local_template(page, origin) + urlsplit(url).path + "?" + urlsplit(url).query
            page.goto(destination, wait_until="domcontentloaded", timeout=90_000)
            wait_ready(page)
            snapshot = state(page)
            assert snapshot["theme"] == theme and not snapshot["overflow"] and snapshot["ariaBusy"] == "false", snapshot
            assert snapshot["updating"] == 0 and snapshot["disabledActiveControls"] == [], snapshot
            page.evaluate("window.scrollTo(0,0)")
            page.screenshot(path=out / f"{theme}-{width}-summary.png", full_page=True)
            results.append({"theme": theme, "viewport": [width, height], "state": snapshot, "errors": errors})
            close_context(page, context, local)
    return results


def capture_sections(browser, url: str, out: Path, local: bool) -> list[dict]:
    origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
    results = []
    for theme in ("light", "dark"):
        for section, suffix in (("demand", "demand-container"), ("team", "team-table")):
            context = clean_context(browser, theme, (1440, 900))
            page = context.new_page()
            destination = url + f"&section={section}"
            if local:
                destination = install_local_template(page, origin) + urlsplit(destination).path + "?" + urlsplit(destination).query
            page.goto(destination, wait_until="domcontentloaded", timeout=90_000)
            wait_ready(page)
            page.locator(f"#tab-{section}.active").wait_for(state="visible", timeout=30_000)
            page.screenshot(path=out / f"{theme}-1440-{suffix}.png", full_page=True)
            results.append({"theme": theme, "section": section, "state": state(page)})
            close_context(page, context, local)
    return results


def axe_scan(browser, url: str, local: bool) -> list[dict]:
    origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
    results = []
    for theme in ("light", "dark"):
        context = clean_context(browser, theme, (1440, 900))
        page = context.new_page()
        destination = url
        if local:
            destination = install_local_template(page, origin) + urlsplit(url).path + "?" + urlsplit(url).query
        page.goto(destination, wait_until="domcontentloaded", timeout=90_000)
        wait_ready(page)
        page.add_script_tag(url="https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js")
        audit = page.evaluate("async()=>{const r=await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}});return r.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.length}))}")
        results.append({"theme": theme, "violations": audit})
        close_context(page, context, local)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--local-template", action="store_true")
    parser.add_argument("--output", default=str(ROOT / "reports" / "commercial-dashboard-v4"))
    args = parser.parse_args()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        report = {
            "p0": assert_p0(browser, args.url, args.local_template),
            "matrix": capture_matrix(browser, args.url, Path(args.output), args.local_template),
            "sections": capture_sections(browser, args.url, Path(args.output), args.local_template),
            "axe": axe_scan(browser, args.url, args.local_template),
        }
        browser.close()
    Path(args.output).mkdir(parents=True, exist_ok=True)
    (Path(args.output) / "qa-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
