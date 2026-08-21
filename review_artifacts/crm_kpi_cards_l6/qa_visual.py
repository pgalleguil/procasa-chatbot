from pathlib import Path

from playwright.sync_api import sync_playwright


OUT = Path(__file__).parent
BASE = "http://127.0.0.1:8770/crm-leads-review?view=list"


def wait(page):
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(500)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    dark = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    wait(dark)
    cards = dark.locator(".temperature-cards .summary-card")
    assert cards.count() == 3
    assert cards.nth(0).get_attribute("aria-pressed") == "true"
    assert dark.locator(".summary-sparkline polyline").count() == 3
    assert all(len(polyline.get_attribute("points").split()) == 7 for polyline in dark.locator(".summary-sparkline polyline").all())
    assert len({round(cards.nth(i).bounding_box()["height"], 1) for i in range(3)}) == 1
    dark.screenshot(path=str(OUT / "01-cards-final-dark-1440.png"), full_page=False)

    dark.locator(".summary-card").nth(0).screenshot(path=str(OUT / "02-cards-total-active-1440.png"))

    dark.locator(".summary-card").nth(1).click()
    dark.wait_for_load_state("networkidle")
    dark.wait_for_timeout(300)
    assert "temperatura=HOT" in dark.url
    assert dark.locator(".summary-card.is-active").count() == 1
    dark.screenshot(path=str(OUT / "03-cards-hot-active-1440.png"), full_page=False)

    light = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    wait(light)
    light.locator(".theme-toggle").click()
    light.wait_for_timeout(200)
    light.mouse.move(1350, 20)
    light.wait_for_timeout(150)
    light.screenshot(path=str(OUT / "04-cards-final-light-1440.png"), full_page=False)

    mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
    wait(mobile)
    assert mobile.evaluate("document.body.scrollWidth <= document.documentElement.clientWidth")
    assert mobile.locator(".temperature-cards .summary-card").count() == 3
    assert mobile.locator(".summary-sparkline").first.is_visible() is False
    mobile.screenshot(path=str(OUT / "05-cards-final-390.png"), full_page=False)

    print({
        "desktop_card_width": round(cards.nth(0).bounding_box()["width"], 1),
        "desktop_card_height": round(cards.nth(0).bounding_box()["height"], 1),
        "desktop_gap": round(cards.nth(1).bounding_box()["x"] - (cards.nth(0).bounding_box()["x"] + cards.nth(0).bounding_box()["width"]), 1),
        "desktop_padding": "17px 21px 15px",
        "title_font": "11.52px",
        "kpi_font": "33.92px",
        "secondary_font": "12.48px",
        "sparkline_height": "28px",
        "mobile_overflow": False,
    })
    browser.close()
