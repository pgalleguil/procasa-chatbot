from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent
URL = "http://127.0.0.1:8770/crm-leads-review?view=list"

def shot(page, name):
    page.screenshot(path=str(OUT / name), full_page=False)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    desktop = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    desktop.goto(URL, wait_until="networkidle")
    desktop.wait_for_timeout(900)
    desktop.screenshot(path=str(OUT / "list-v2-1440.png"), full_page=False)
    desktop.locator(".summary-card").filter(has_text="Lead Hot").click()
    desktop.wait_for_load_state("networkidle")
    shot(desktop, "list-v2-hot-filter.png")
    desktop.goto(URL, wait_until="networkidle")
    desktop.wait_for_timeout(900)
    desktop.locator(".state-metric").filter(has_text="En gestión").click()
    desktop.wait_for_load_state("networkidle")
    shot(desktop, "list-v2-status-filter.png")
    desktop.goto(URL, wait_until="networkidle")
    desktop.wait_for_timeout(900)
    desktop.locator("[data-quick-management]").first.click()
    desktop.wait_for_timeout(250)
    shot(desktop, "quick-response-1440.png")
    desktop.locator('[data-quick-result="NOT_INTERESTED"]').click()
    desktop.wait_for_timeout(100)
    shot(desktop, "quick-response-reason-1440.png")
    mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
    mobile.goto(URL, wait_until="networkidle")
    mobile.wait_for_timeout(900)
    mobile.screenshot(path=str(OUT / "list-v2-390.png"), full_page=False)
    mobile.locator("[data-quick-management]").first.click()
    mobile.wait_for_timeout(250)
    mobile.screenshot(path=str(OUT / "quick-response-390.png"), full_page=False)
    print({
        "desktop_cards": desktop.locator(".temperature-cards .summary-card").count(),
        "desktop_headers": desktop.locator("thead th").all_text_contents(),
        "desktop_rows_hot": desktop.locator("tbody tr.clickable-row").count(),
        "desktop_modal_buttons": desktop.locator("#quickResultGrid [data-quick-result]").count(),
        "mobile_overflow": mobile.locator("body").evaluate("el => el.scrollWidth > el.clientWidth"),
        "mobile_card_height": mobile.locator("tbody tr.clickable-row").first.bounding_box()["height"],
    })
    browser.close()
