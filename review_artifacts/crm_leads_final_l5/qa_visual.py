from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent
BASE = "http://127.0.0.1:8770/crm-leads-review?view=list"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(1500)
    assert page.locator(".crm-table th").all_text_contents()[0] == "Asignado"
    assert page.locator(".summary-sparkline").count() == 3
    page.screenshot(path=str(OUT / "01-list-l5-1440.png"), full_page=False)
    page.screenshot(path=str(OUT / "02-cards-sparkline-1440.png"), full_page=False)

    page.locator(".summary-card").nth(1).click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    assert "temperatura=HOT" in page.url
    assert page.locator("select[name=temperatura]").input_value() == "HOT"
    page.screenshot(path=str(OUT / "03-hot-filter-fixed-1440.png"), full_page=False)

    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(1500)
    page.locator("[data-quick-management]").nth(4).click()
    page.wait_for_timeout(300)
    assert page.locator("#quickResultGrid [data-quick-result]").count() == 5
    page.screenshot(path=str(OUT / "04-priority-managed-1440.png"), full_page=False)
    page.screenshot(path=str(OUT / "05-quick-final-1440.png"), full_page=False)
    page.locator('[data-quick-result="NOT_INTERESTED"]').click()
    page.wait_for_timeout(150)
    page.screenshot(path=str(OUT / "06-quick-reason-1440.png"), full_page=False)
    light = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    light.goto(BASE, wait_until="networkidle")
    light.wait_for_timeout(1500)
    light.locator(".theme-toggle").click()
    light.locator("[data-quick-management]").first.click()
    light.wait_for_timeout(300)
    light.screenshot(path=str(OUT / "10-quick-final-light-1440.png"), full_page=False)

    mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
    mobile.goto(BASE, wait_until="networkidle")
    mobile.wait_for_timeout(1500)
    assert mobile.evaluate("document.body.scrollWidth <= document.documentElement.clientWidth")
    mobile.screenshot(path=str(OUT / "07-list-l5-390.png"), full_page=False)
    mobile.locator("[data-quick-management]").first.click()
    mobile.wait_for_timeout(300)
    assert mobile.locator("#quickResultGrid [data-quick-result]").count() == 5
    mobile.screenshot(path=str(OUT / "08-quick-final-390.png"), full_page=False)
    mobile.locator('[data-quick-result="OTHER_EXPLICIT"]').click()
    mobile.locator("#quickOtherOutcome").fill("Cliente continuará evaluando alternativas")
    mobile.screenshot(path=str(OUT / "09-quick-other-390.png"), full_page=False)
    print({"desktop_sparkline_buckets": page.locator(".summary-sparkline span").count(), "mobile_overflow": False})
    browser.close()
