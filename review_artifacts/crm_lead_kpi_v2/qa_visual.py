from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent
BASE = "http://127.0.0.1:8770/crm-leads-review?view=list"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(1600)
    cards = page.locator(".temperature-cards .summary-card")
    boxes = [cards.nth(i).bounding_box() for i in range(3)]
    assert len({round(box["width"], 1) for box in boxes}) == 1
    assert len({round(box["height"], 1) for box in boxes}) == 1
    assert not page.locator(".operational-alerts").count()
    page.screenshot(path=str(OUT / "01-kpi-bar-1440.png"), full_page=False)

    print("card hrefs", page.locator(".temperature-cards .summary-card").evaluate_all("els => els.map(el => el.href)"))
    hot_card = page.locator(".temperature-cards .summary-card").nth(1)
    hot_card.click(force=True)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1600)
    print("hot url", page.url)
    assert "temperatura=HOT" in page.url
    assert page.locator(".summary-card.is-active").filter(has_text="Lead Hot").count() == 1
    page.screenshot(path=str(OUT / "02-kpi-hot-active-1440.png"), full_page=False)

    page.goto(BASE, wait_until="networkidle")
    page.locator(".state-metric").filter(has_text="Sin atender").click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1600)
    assert "estado=NEW" in page.url
    assert page.locator(".state-metric.is-active").filter(has_text="Sin atender").count() == 1
    page.screenshot(path=str(OUT / "03-kpi-status-active-1440.png"), full_page=False)

    mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
    mobile.goto(BASE, wait_until="networkidle")
    mobile.wait_for_timeout(1600)
    assert mobile.locator(".temperature-cards .summary-card").count() == 3
    assert mobile.evaluate("document.body.scrollWidth <= document.documentElement.clientWidth")
    mobile.screenshot(path=str(OUT / "04-kpi-bar-390.png"), full_page=False)
    print({"desktop_card_box": boxes[0], "mobile_overflow": False, "desktop_url": page.url})
    browser.close()
