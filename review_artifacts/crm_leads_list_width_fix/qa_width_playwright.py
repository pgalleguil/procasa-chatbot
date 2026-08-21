"""Controlled viewport QA for the sanitized CRM list review."""

import json
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8770/crm-leads-review?view=list"


def rect(page, selector):
    return page.locator(selector).first.evaluate(
        """el => { const r=el.getBoundingClientRect(), s=getComputedStyle(el);
        return {x:r.x,y:r.y,width:r.width,height:r.height,padding:s.padding,
        transform:s.transform,zoom:s.zoom}; }"""
    )


def run_case(browser, filename, width, height):
    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
    page = context.new_page()
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(250)
    data = page.evaluate(
        """() => ({innerWidth, innerHeight, dpr:devicePixelRatio,
        clientWidth:document.documentElement.clientWidth,
        scrollWidth:document.documentElement.scrollWidth,
        bodyWidth:document.body.getBoundingClientRect().width,
        viewportMeta:[...document.querySelectorAll('meta[name="viewport"]')].map(e=>e.content),
        ctaVisible:[...document.querySelectorAll('.quick-management-btn')].every(e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth})})"""
    )
    data.update({
        "viewport": f"{width}x{height}",
        "sidebar": rect(page, ".sidebar"),
        "main": rect(page, ".main-content"),
        "list": rect(page, "#crmDynamicContent"),
        "container": rect(page, ".table-container"),
        "toolbar": rect(page, "#crmFilterForm"),
        "table": rect(page, ".crm-table"),
    })
    ancestors = page.locator("#crmDynamicContent").evaluate(
        """el => { const out=[]; for(let n=el;n;n=n.parentElement){const s=getComputedStyle(n);out.push({tag:n.tagName,id:n.id,transform:s.transform,zoom:s.zoom});} return out; }"""
    )
    data["listAncestors"] = ancestors
    assert (data["innerWidth"], data["innerHeight"], data["dpr"]) == (width, height, 1)
    assert data["clientWidth"] == width and data["scrollWidth"] <= width
    assert data["ctaVisible"] and data["viewportMeta"] == ["width=device-width, initial-scale=1.0"]
    assert all(item["transform"] == "none" and item["zoom"] == "1" for item in ancestors)
    main_padding = float(data["main"]["padding"].split()[0].replace("px", ""))
    available = data["main"]["width"] - main_padding * 2
    assert data["container"]["width"] >= available * 0.92 if width >= 1024 else data["list"]["width"] >= available * 0.94
    path = ROOT / filename
    page.screenshot(path=str(path), full_page=False)
    assert Image.open(path).size == (width, height)
    context.close()
    return data


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    results = [run_case(browser, "01-list-width-1440.png", 1440, 1000),
               run_case(browser, "02-list-width-1024.png", 1024, 768),
               run_case(browser, "03-list-width-390.png", 390, 844)]
    browser.close()
print(json.dumps(results, ensure_ascii=False, indent=2))
