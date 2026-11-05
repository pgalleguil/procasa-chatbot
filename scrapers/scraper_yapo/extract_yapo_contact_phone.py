"""Post-scraping enrichment: extract contact phone from Yapo listing pages.

Run after main scraper to populate contact_phone on DUEÑO_SEGURO / INCIERTO docs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from config import get_config
except ImportError:  # importado como paquete desde el servidor/tests
    from scrapers.scraper_yapo.config import get_config

try:
    from captacion_contact_identity import normalize_phone
except ImportError:  # ejecución directa desde scrapers/scraper_yapo/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from captacion_contact_identity import normalize_phone

try:
    from pymongo import MongoClient, DESCENDING
except ImportError:
    MongoClient = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

try:
    from proxy_manager import ProxyManager, load_proxies_from_env
except ImportError:
    ProxyManager = None  # type: ignore
    load_proxies_from_env = lambda: []  # type: ignore

LOGGER = logging.getLogger("extract_contact_phone")

ROOT_DIR = Path(__file__).resolve().parent
HTML_DUMPS_DIR = ROOT_DIR / "html_dumps"

CONTACT_PHONE_SCHEMA = {
    "status": "PENDING",
    "raw": "",
    "normalized": "",
    "source": "",
    "method": "",
    "attempted_at": None,
    "last_error": "",
}

CHILEAN_PHONE_RE = re.compile(
    r"(?:(?:56|\\+56)?\s*)?(\d{1,2}\s*)?(?:9\s*)?\d{4}\s*\d{4}"
)

CHILEAN_PHONE_CLEAN = re.compile(r"[^\d+]")

# Patterns for local text phone scanning (ordered: labelled first, then bare)
LOCAL_PHONE_PATTERNS = [
    # Labelled patterns
    (r'(?:fono|tel[eé]fono|celular|whatsapp|wsp|contacto|llamar al|movil|m[oó]vil)\s*[:.\s-]*(\+?56\s*9\s*\d{4}\s*\d{4})', re.I),
    (r'(?:fono|tel[eé]fono|celular|whatsapp|wsp|contacto|llamar al|movil|m[oó]vil)\s*[:.\s-]*(9\s*\d{4}\s*\d{4})', re.I),
    (r'(?:fono|tel[eé]fono|celular|whatsapp|wsp|contacto|llamar al|movil|m[oó]vil)\s*[:.\s-]*(56\s*9\s*\d{4}\s*\d{4})', re.I),
    # Bare Chilean mobile patterns (avoid false positives by requiring word boundary)
    (r'(?<!\d)(\+56\s*9\s*\d{4}\s*\d{4})(?!\d)', re.I),
    (r'(?<!\d)(56\s*9\s*\d{4}\s*\d{4})(?!\d)', re.I),
    (r'(?<!\d)(9\s*\d{4}\s*\d{4})(?!\d)', re.I),
]

# Fields in MongoDB doc to scan for phone text
LOCAL_SCAN_FIELDS = [
    "title",
    "description",
    "seller_name",
    "seller_text",
    "raw_attributes",
]


def _md5_url(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def _html_path_for_url(url: str) -> Path:
    return HTML_DUMPS_DIR / f"{_md5_url(url)}.html"


def _normalize_chilean_phone(raw: str) -> str:
    """Delegate canonicalization to the single strict phone normalizer.

    Extraction regexes remain local to Yapo; identity representation does not.
    The stored form is digits-only, matching CRM and contact-identity lookup.
    """
    return normalize_phone(raw)


def _phone_root_fields(contact_phone: dict[str, Any] | None) -> dict[str, Any]:
    """Build canonical root fields for a valid scraper-discovered phone."""
    if not isinstance(contact_phone, dict) or contact_phone.get("status") != "FOUND":
        return {}
    raw = str(contact_phone.get("raw") or contact_phone.get("normalized") or "").strip()
    normalized = normalize_phone(raw)
    if not normalized:
        return {}
    return {
        "phone_original_value": raw,
        "telefono_normalizado": normalized,
        "phone_normalized": normalized,
        "phone_source": "SCRAPER",
        "phone_added_at": contact_phone.get("attempted_at") or _now_iso(),
        "phone_version": 1,
    }


def _scan_text_for_phone(text: str) -> list[dict[str, str]]:
    """Scan a single text string for Chilean mobile phone patterns.
    Returns list of hits with raw, normalized, and matched_pattern.
    """
    hits = []
    for pat, flags in LOCAL_PHONE_PATTERNS:
        for m in re.finditer(pat, text, flags):
            raw = m.group(1).strip()
            raw_clean = CHILEAN_PHONE_CLEAN.sub("", raw)
            if len(raw_clean) >= 8 and len(raw_clean) <= 13:
                normalized = _normalize_chilean_phone(raw)
                if normalized:
                    hits.append({
                        "raw": raw,
                        "normalized": normalized,
                        "matched_pattern": pat.pattern[:50],
                    })
    return hits


def _scan_text_fields_for_phone(doc: dict[str, Any], html: str | None = None) -> dict[str, Any]:
    """Scan MongoDB text fields and optional HTML for Chilean phone numbers.
    Returns dict with found, raw, normalized, source_field, hits, evidence_fields.
    """
    result = {
        "found": False,
        "raw": "",
        "normalized": "",
        "source_field": "",
        "hits": [],
        "evidence_fields": [],
    }

    # Scan Mongo fields
    for field in LOCAL_SCAN_FIELDS:
        value = doc.get(field, "")
        if not value:
            continue
        if isinstance(value, dict):
            text = " ".join(str(v) for v in value.values() if v)
        elif isinstance(value, list):
            text = " ".join(str(v) for v in value)
        else:
            text = str(value)
        if not text:
            continue
        hits = _scan_text_for_phone(text)
        if hits:
            result["found"] = True
            result["raw"] = hits[0]["raw"]
            result["normalized"] = hits[0]["normalized"]
            result["source_field"] = field
            result["hits"].extend(hits)
            result["evidence_fields"].append(field)

    # Scan publisher_identity_candidates
    pic = doc.get("publisher_identity_candidates", [])
    if pic and isinstance(pic, list):
        for item in pic:
            if isinstance(item, dict):
                text = " ".join(str(v) for v in item.values() if v)
            else:
                text = str(item)
            hits = _scan_text_for_phone(text)
            if hits:
                result["found"] = True
                result["raw"] = hits[0]["raw"]
                result["normalized"] = hits[0]["normalized"]
                result["source_field"] = "publisher_identity_candidates"
                result["hits"].extend(hits)
                result["evidence_fields"].append("publisher_identity_candidates")

    # Scan local HTML
    if html is not None:
        text = html
        # Remove script and style blocks to reduce noise
        text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        hits = _scan_text_for_phone(text)
        if hits:
            # Deduplicate against field hits
            existing_norm = {h["normalized"] for h in result["hits"]}
            new_hits = [h for h in hits if h["normalized"] not in existing_norm]
            if new_hits:
                if not result["found"]:
                    result["found"] = True
                    result["raw"] = new_hits[0]["raw"]
                    result["normalized"] = new_hits[0]["normalized"]
                    result["source_field"] = "html_local"
                result["hits"].extend(new_hits)
                result["evidence_fields"].append("html_local")

    return result


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_contact_phone_doc(
    status: str,
    raw: str = "",
    normalized: str = "",
    source: str = "",
    method: str = "",
    last_error: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "raw": raw,
        "normalized": normalized,
        "source": source,
        "method": method,
        "attempted_at": _now_iso(),
        "last_error": last_error,
    }


def _find_show_phone_in_html(html: str) -> dict[str, str] | None:
    m = re.search(
        r'<div[^>]*class="show-phone"[^>]*data-contact="([^"]+)"[^>]*>',
        html,
        re.I,
    )
    if m:
        return {"data_contact": m.group(1)}
    m2 = re.search(
        r'data-contact="([^"]+)"',
        html,
        re.I,
    )
    if m2:
        return {"data_contact": m2.group(1)}
    return None


def _find_whatsapp_in_html(html: str) -> bool:
    return bool(re.search(r'whatsapp|fa-whatsapp|whatsapp-btn', html, re.I))


def _find_form_indicators(html: str) -> list[str]:
    indicators = []
    if re.search(r'id="cnmessage_fromemail"|name="cnmessage_fromemail"', html):
        indicators.append("cnmessage_fromemail")
    if re.search(r'id="cnmessage_name"|name="cnmessage_name"', html):
        indicators.append("cnmessage_name")
    if re.search(r'cnmessage\[phone\]\[combined\]', html):
        indicators.append("cnmessage_phone")
    if re.search(r'id="cnmessage_extra_fields_rut"|name="cnmessage_extra_fields_rut"', html):
        indicators.append("cnmessage_rut")
    if re.search(r'g-recaptcha|recaptcha', html, re.I):
        indicators.append("recaptcha")
    return indicators


def _is_show_phone_disabled(html: str) -> bool:
    if re.search(r'pointer-events:\s*none', html, re.I):
        return True
    return False


import random
import string
import secrets

FAKE_FIRST_NAMES = [
    "Andres", "Beatriz", "Carlos", "Daniela", "Eduardo",
    "Francisca", "Gonzalo", "Hector", "Isabel", "Javier",
    "Karen", "Luis", "Marcela", "Nicolas", "Olga",
    "Pablo", "Raquel", "Ricardo", "Sofia", "Tomas",
    "Valentina", "Cristian", "Camila", "Felipe", "Rocio",
    "Alvaro", "Paula", "Sergio", "Angela", "Mauricio",
]

FAKE_LAST_NAMES = [
    "Aguirre", "Barrios", "Contreras", "Diaz", "Espinoza",
    "Fernandez", "Gutierrez", "Herrera", "Ibañez", "Jimenez",
    "Lara", "Martinez", "Navarro", "Ortega", "Paredes",
    "Quintana", "Ramirez", "Salinas", "Tapia", "Ulloa",
    "Valenzuela", "Zapata", "Castro", "Reyes", "Muñoz",
    "Sepulveda", "Vargas", "Rojas", "Silva", "Peña",
]

EMAIL_DOMAINS = [
    "gmail.com", "outlook.cl", "yahoo.cl", "hotmail.com",
    "live.cl", "correo.cl", "gmail.cl",
]


def _generate_fake_contact_data() -> dict[str, str]:
    """Generate unique synthetic contact data per listing submission."""
    suffix = secrets.token_hex(4)
    domain = random.choice(EMAIL_DOMAINS)
    email = f"c.{suffix}@{domain}"
    name = f"{random.choice(FAKE_FIRST_NAMES)} {random.choice(FAKE_LAST_NAMES)}"
    phone = f"+569{random.randint(20000000, 99999999)}"
    rut_body = random.randint(12000000, 29999999)
    rut_dv = "0123456789K"[rut_body % 11]
    rut = f"{rut_body}-{rut_dv}"
    return {"email": email, "name": name, "phone": phone, "rut": rut}


def _extract_form_id(html: str) -> str | None:
    m = re.search(r'id="messageform_([a-f0-9]+)"', html)
    if m:
        return m.group(1)
    return None


def _extract_phone_from_response_body(body: str) -> str | None:
    # Try JSON response first: {"content":"+569XXXXXXXX","success":true} or {"content":"8448083","success":true}
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            content = data.get("content", "")
            if content and data.get("success"):
                cleaned = CHILEAN_PHONE_CLEAN.sub("", content)
                if len(cleaned) >= 6 and len(cleaned) <= 15:
                    return content
    except (json.JSONDecodeError, TypeError):
        pass

    patterns = [
        r'content["\']?\s*[:=]\s*["\'](\+?56\s*9\s*\d{4}\s*\d{4})["\']',
        r'content["\']?\s*[:=]\s*["\'](56\s*9\s*\d{4}\s*\d{4})["\']',
        r'content["\']?\s*[:=]\s*["\'](9\s*\d{4}\s*\d{4})["\']',
        r'(?:"phone"|"telefono")\s*[:=]\s*"(\+?56\s*9\s*\d{4}\s*\d{4})"',
        r'(?:"phone"|"telefono")\s*[:=]\s*"(56\s*9\s*\d{4}\s*\d{4})"',
        r'(?:"phone"|"telefono")\s*[:=]\s*"(9\s*\d{4}\s*\d{4})"',
        r"(?:phone|telefono|fono|celular)\s*[:\s]+([\+\d\s\-]{7,20})",
        r"(\+56\s*9\s*\d{4}\s*\d{4})",
        r"(?<!\d)(56\s*9\s*\d{4}\s*\d{4})(?!\d)",
        r"(?<!\d)(9\s*\d{4}\s*\d{4})(?!\d)",
    ]
    for pat in patterns:
        m = re.search(pat, body, re.I)
        if m:
            candidate = m.group(1).strip()
            cleaned = CHILEAN_PHONE_CLEAN.sub("", candidate)
            if cleaned and len(cleaned) >= 8 and len(cleaned) <= 13:
                return candidate
    return None


def _fetch_data_contact(url: str, timeout: int = 15) -> dict[str, Any]:
    """Make an HTTP request to data-contact endpoint to get phone."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.7",
        "Referer": "https://www.yapo.cl/",
    }
    try:
        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            phone = _extract_phone_from_response_body(body)
            return {
                "success": phone is not None,
                "phone": phone or "",
                "body": body[:2000],
                "status_code": resp.status,
            }
    except Exception as exc:
        return {"success": False, "phone": "", "body": "", "status_code": 0, "error": str(exc)}


def _extract_via_playwright(
    url: str,
    data_contact: str | None = None,
    timeout: int = 20_000,
) -> dict[str, Any]:
    """Use Playwright to load the listing page, click show-phone, and capture the response."""
    if sync_playwright is None:
        return {"success": False, "error": "playwright not installed", "phone": ""}

    captured: dict[str, Any] = {"phone": "", "success": False, "error": ""}

    def handle_response(response):
        nonlocal captured
        if "clickhouse_mvp.php/contacts" in response.url or "contacts" in response.url:
            try:
                body = response.text()
                phone = _extract_phone_from_response_body(body)
                if phone:
                    captured["phone"] = phone
                    captured["success"] = True
            except Exception:
                pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                locale="es-CL",
            )
            page = context.new_page()
            page.on("response", handle_response)

            page.goto(url, wait_until="domcontentloaded", timeout=timeout)

            try:
                show_phone_btn = page.wait_for_selector(
                    ".show-phone", timeout=8000
                )
                if show_phone_btn:
                    show_phone_btn.click()
                    page.wait_for_timeout(3000)

                    phone_from_dom = _extract_phone_from_response_body(page.content())
                    if phone_from_dom:
                        captured["phone"] = phone_from_dom
                        captured["success"] = True
                        captured["source"] = "dom_after_click"
            except Exception:
                captured.setdefault("warnings", []).append("show-phone button not clickable")

            if not captured["success"] and data_contact:
                try:
                    page.goto(data_contact, wait_until="domcontentloaded", timeout=timeout)
                    body = page.content()
                    phone = _extract_phone_from_response_body(body)
                    if phone:
                        captured["phone"] = phone
                        captured["success"] = True
                        captured["source"] = "data_contact_via_playwright"
                except Exception:
                    pass

            browser.close()
    except Exception as exc:
        captured["error"] = str(exc)

    return captured


def _parse_playwright_proxy(proxy_url: str) -> dict[str, str]:
    """Convert a proxy URL like http://user:pass@host:port to Playwright proxy dict."""
    from urllib.parse import urlparse
    parsed = urlparse(proxy_url)
    pw_proxy: dict[str, str] = {"server": f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port or 80}"}
    if parsed.username:
        pw_proxy["username"] = parsed.username
    if parsed.password:
        pw_proxy["password"] = parsed.password
    return pw_proxy


def _load_proxy_for_playwright() -> dict | None:
    """Load a proxy from env/proxy_manager config, return Playwright-compatible dict or None."""
    proxies = load_proxies_from_env()
    if not proxies:
        return None
    chosen = random.choice(proxies)
    return _parse_playwright_proxy(chosen)


def _extract_via_form_submission(
    url: str,
    data_contact: str | None = None,
    timeout: int = 30_000,
    proxy_config: dict | None = None,
) -> dict[str, Any]:
    """Use Playwright to fill and submit the contact form, then extract phone from the response."""
    if sync_playwright is None:
        return {"success": False, "error": "playwright not installed", "phone": ""}

    captured: dict[str, Any] = {"phone": "", "success": False, "error": ""}
    fake_data = _generate_fake_contact_data()

    def handle_response(response):
        nonlocal captured
        if "/cnmessage/send/" not in response.url:
            return
        body = ""
        try:
            body = response.text()
        except Exception:
            return
        if not body:
            return
        phone = _extract_phone_from_response_body(body)
        if phone:
            captured["phone"] = phone
            captured["success"] = True
            captured["response_url"] = response.url

    def handle_request(request):
        nonlocal captured
        if "/cnmessage/send/" in request.url and request.method == "POST":
            captured["last_post_url"] = request.url

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx_kwargs: dict[str, Any] = {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "locale": "es-CL",
                "viewport": {"width": 1280, "height": 720},
            }
            if proxy_config:
                ctx_kwargs["proxy"] = proxy_config
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()
            page.on("response", handle_response)
            page.on("request", handle_request)

            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_timeout(3000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)

            # Wait for fpPromise (FingerprintJS) to be ready
            fp_ready = page.evaluate("""() => {
                return new Promise((resolve) => {
                    if (typeof fpPromise !== 'undefined') {
                        resolve(true);
                    } else {
                        let t = 0;
                        const c = setInterval(() => {
                            t++;
                            if (typeof fpPromise !== 'undefined') { clearInterval(c); resolve(true); }
                            if (t > 15) { clearInterval(c); resolve(false); }
                        }, 500);
                    }
                });
            }""")
            LOGGER.debug("FingerprintJS ready: %s", fp_ready)

            # Fill all form fields using Playwright fill() for proper event dispatch
            try:
                email_el = page.query_selector('#cnmessage_fromemail')
                if email_el: email_el.fill(fake_data["email"])

                name_el = page.query_selector('#cnmessage_name')
                if name_el: name_el.fill(fake_data["name"])

                phone_input = page.query_selector('#cnmessage\\[phone\\]\\[combined\\]')
                if phone_input:
                    phone_input.fill(fake_data["phone"])
                else:
                    cc = page.query_selector('#cnmessage_phone_countrycode')
                    if cc: cc.fill('0056')
                    pn = page.query_selector('#cnmessage_phone_phonenumber')
                    if pn: pn.fill(fake_data["phone"].replace('+569', ''))

                rut_el = page.query_selector('#cnmessage_extra_fields_rut')
                if rut_el: rut_el.fill(fake_data["rut"])

                msg_el = page.query_selector('#cnmessage_message')
                if msg_el: msg_el.fill(f'Me interesa este aviso. Favor contactarme.')
            except Exception as exc:
                captured["error"] = f"fill failed: {exc}"
                browser.close()
                return captured

            page.wait_for_timeout(1000)

            # Trigger show-phone via jQuery click on .show-phone
            click_result = page.evaluate("""() => {
                const btn = document.querySelector('.show-phone');
                if (!btn) return 'no button';
                if (typeof $ === 'undefined') return 'no jquery';
                $(btn).trigger('click');
                return 'clicked';
            }""")
            LOGGER.debug("Show-phone click: %s", click_result)

            # Wait for response with timeout (proxy can be slow)
            page.wait_for_timeout(15000)

            # If no response captured via XHR, poll DOM for phone
            if not captured["success"]:
                content = page.content()
                phone = _extract_phone_from_response_body(content)
                if phone:
                    captured["phone"] = phone
                    captured["success"] = True
                    captured["source"] = "dom_after_submit"

            if not captured["success"]:
                visible_text = page.inner_text("body")
                phone = _extract_phone_from_response_body(visible_text)
                if phone:
                    captured["phone"] = phone
                    captured["success"] = True
                    captured["source"] = "text_after_submit"

            if not captured["success"]:
                try:
                    success_div = page.query_selector(".contact-form .messageform:not(.hide), [class*='success']")
                    if success_div:
                        captured["post_submit_status"] = f"form_sent: {success_div.inner_text()[:200]}"
                except Exception:
                    pass

            browser.close()
    except Exception as exc:
        captured["error"] = str(exc)

    if not captured["success"] and not captured.get("error"):
        captured["error"] = "phone not found after show-phone click"

    captured["fake_data_used"] = fake_data
    return captured


def process_document(
    doc: dict[str, Any],
    *,
    dry_run: bool = False,
    use_playwright: bool = False,
) -> dict[str, Any]:
    """Process a single document and return contact_phone update."""
    listing_id = doc.get("listing_id", "")
    url = doc.get("url", "")
    html_path = _html_path_for_url(url)
    html_exists = html_path.exists()

    result = {
        "listing_id": listing_id,
        "url": url,
        "state": doc.get("classification", {}).get("state", ""),
        "html_exists": html_exists,
        "has_show_phone": False,
        "has_data_contact": False,
        "data_contact_url": "",
        "data_adid": "",
        "has_whatsapp": False,
        "show_phone_disabled": False,
        "form_indicators": [],
        "contact_phone": None,
    }

    if not html_exists:
        result["contact_phone"] = _make_contact_phone_doc(
            "NOT_FOUND", last_error="no local HTML dump"
        )
        return result

    try:
        html = html_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        result["contact_phone"] = _make_contact_phone_doc(
            "ERROR", last_error=f"cannot read HTML: {exc}"
        )
        return result

    show_phone_info = _find_show_phone_in_html(html)
    if show_phone_info:
        result["has_show_phone"] = True
        result["has_data_contact"] = True
        result["data_contact_url"] = show_phone_info.get("data_contact", "")
        data_adid_m = re.search(r'data-adid="(\d+)"', html)
        if data_adid_m:
            result["data_adid"] = data_adid_m.group(1)

    result["has_whatsapp"] = _find_whatsapp_in_html(html)
    result["form_indicators"] = _find_form_indicators(html)
    result["show_phone_disabled"] = _is_show_phone_disabled(html) if result["has_show_phone"] else False

    if not result["has_show_phone"]:
        result["contact_phone"] = _make_contact_phone_doc("NO_BUTTON")
        return result

    dcu = result["data_contact_url"]
    if not dcu:
        result["contact_phone"] = _make_contact_phone_doc(
            "NOT_FOUND", last_error="data-contact URL empty"
        )
        return result

    fetch_result = _fetch_data_contact(dcu)
    if fetch_result["success"] and fetch_result["phone"]:
        raw_phone = fetch_result["phone"]
        normalized = _normalize_chilean_phone(raw_phone)
        result["contact_phone"] = _make_contact_phone_doc(
            "FOUND",
            raw=raw_phone,
            normalized=normalized,
            source="show_phone_data_contact",
            method="http_request",
        )
        return result

    if use_playwright:
        pw_result = _extract_via_playwright(url, data_contact=dcu)
        if pw_result["success"] and pw_result["phone"]:
            raw_phone = pw_result["phone"]
            normalized = _normalize_chilean_phone(raw_phone)
            source_label = pw_result.get("source", "show_phone_click")
            result["contact_phone"] = _make_contact_phone_doc(
                "FOUND",
                raw=raw_phone,
                normalized=normalized,
                source=source_label,
                method="playwright_click",
            )
            return result
        if pw_result.get("error"):
            result["contact_phone"] = _make_contact_phone_doc(
                "ERROR",
                last_error=f"playwright: {pw_result['error']}",
            )
            return result

    if result["form_indicators"] or result["show_phone_disabled"]:
        evidence_parts = []
        if result["show_phone_disabled"]:
            evidence_parts.append("show_phone_button_disabled")
        if result["form_indicators"]:
            evidence_parts.append(f"form_fields:{','.join(result['form_indicators'])}")
        result["contact_phone"] = _make_contact_phone_doc(
            "FORM_REQUIRED",
            last_error="; ".join(evidence_parts),
        )
        return result

    result["contact_phone"] = _make_contact_phone_doc(
        "NOT_FOUND",
        last_error=f"data-contact returned no phone (status={fetch_result.get('status_code','?')})",
    )
    return result


_PROXY_CACHE: list | None = None


def process_document_with_form_submission(
    doc: dict[str, Any],
    no_proxy: bool = False,
) -> dict[str, Any]:
    """Process document by submitting the contact form via Playwright to extract phone."""
    listing_id = doc.get("listing_id", "")
    url = doc.get("url", "")
    html_path = _html_path_for_url(url)
    html_exists = html_path.exists()

    result = {
        "listing_id": listing_id,
        "url": url,
        "state": doc.get("classification", {}).get("state", ""),
        "html_exists": html_exists,
        "contact_phone": None,
        "fake_data_used": {},
    }

    proxy_config = None
    if not no_proxy:
        proxy_config = _load_proxy_for_playwright()

    pw_result = _extract_via_form_submission(url, proxy_config=proxy_config)

    if pw_result["success"] and pw_result["phone"]:
        raw_phone = pw_result["phone"]
        normalized = _normalize_chilean_phone(raw_phone)
        result["contact_phone"] = _make_contact_phone_doc(
            "FOUND",
            raw=raw_phone,
            normalized=normalized,
            source="form_submission",
            method="playwright_form_fill_and_submit",
        )
    elif pw_result.get("error"):
        result["contact_phone"] = _make_contact_phone_doc(
            "ERROR",
            last_error=pw_result["error"],
        )
    else:
        result["contact_phone"] = _make_contact_phone_doc(
            "NOT_FOUND",
            last_error="form submitted but no phone found in response",
        )

    result["fake_data_used"] = pw_result.get("fake_data_used", {})
    return result


def process_document_local_scan(
    doc: dict[str, Any],
) -> dict[str, Any]:
    """Process document by scanning existing local text for phone numbers.
    Only returns FOUND if phone is found; otherwise returns SKIPPED with a note.
    """
    listing_id = doc.get("listing_id", "")
    url = doc.get("url", "")
    html_path = _html_path_for_url(url)
    html_exists = html_path.exists()

    result = {
        "listing_id": listing_id,
        "url": url,
        "state": doc.get("classification", {}).get("state", ""),
        "html_exists": html_exists,
        "contact_phone": None,
        "phone_hits": [],
        "evidence_fields": [],
    }

    html = None
    if html_exists:
        try:
            html = html_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

    scan_result = _scan_text_fields_for_phone(doc, html)

    if scan_result["found"]:
        phone = scan_result
        result["phone_hits"] = phone["hits"]
        result["evidence_fields"] = phone["evidence_fields"]
        result["contact_phone"] = _make_contact_phone_doc(
            "FOUND",
            raw=phone["raw"],
            normalized=phone["normalized"],
            source="local_text_regex",
            method="mongo_fields_or_local_html_scan",
        )
    else:
        result["contact_phone"] = _make_contact_phone_doc(
            "SKIPPED",
            last_error="no phone found in local text scan",
        )

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract contact phone from Yapo listings for CRM enrichment."
    )
    parser.add_argument("--limit", type=int, default=10, help="Max documents to process")
    parser.add_argument("--dry-run", action="store_true", help="Diagnostic mode, no writes")
    parser.add_argument("--sleep", type=float, default=2.0, help="Seconds between requests")
    parser.add_argument("--max-errors", type=int, default=10, help="Stop after N consecutive errors")
    parser.add_argument("--use-playwright", action="store_true", help="Enable Playwright fallback")
    parser.add_argument("--local-text-scan", action="store_true", help="Only scan existing text fields / local HTML for phone numbers")
    parser.add_argument("--submit-form", action="store_true", help="Use Playwright to fill contact form with generated data and extract phone from response")
    parser.add_argument("--skip-existing", action="store_true", help="Skip docs that already have any contact_phone status")
    parser.add_argument("--no-proxy", action="store_true", help="Disable proxy for Playwright (use direct connection)")
    parser.add_argument("--state", choices=["DUEÑO_SEGURO", "INCIERTO", "ALL"], default="ALL", help="Filter by classification.state (default: ALL = both)")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cfg = get_config()
    if MongoClient is None:
        LOGGER.error("pymongo is not installed")
        return 1

    client = MongoClient(cfg.mongo_uri)
    col = client[cfg.mongo_db][cfg.mongo_collection]

    # Base filter: only DUEÑO_SEGURO and INCIERTO (or specific state if --state is set)
    if args.state == "ALL":
        state_filter = ["DUEÑO_SEGURO", "INCIERTO"]
    else:
        state_filter = [args.state]

    query: dict[str, Any] = {
        "classification.state": {"$in": state_filter},
    }

    if args.submit_form or args.use_playwright:
        query["$or"] = [
            {"contact_phone": {"$exists": False}},
            {"contact_phone.status": {"$in": ["PENDING", "ERROR", "FORM_REQUIRED", "NO_BUTTON", "NOT_FOUND"]}},
        ]
    elif args.local_text_scan:
        # Local text scan: only skip FOUND (re-scan PENDING/ERROR/FORM_REQUIRED/NO_BUTTON)
        if args.skip_existing:
            query["$or"] = [
                {"contact_phone": {"$exists": False}},
                {"contact_phone.status": {"$in": ["PENDING", "ERROR"]}},
            ]
        else:
            # Don't re-process docs already marked FOUND
            query["$or"] = [
                {"contact_phone": {"$exists": False}},
                {"contact_phone.status": {"$ne": "FOUND"}},
            ]
    else:
        # Original mode: only process docs without contact_phone or PENDING/ERROR
        query["$or"] = [
            {"contact_phone": {"$exists": False}},
            {"contact_phone.status": {"$in": ["PENDING", "ERROR"]}},
        ]

    total_candidates = col.count_documents(query)
    LOGGER.info("Candidates matching filter: %d", total_candidates)

    docs = list(col.find(query).limit(args.limit).sort("processed_at", DESCENDING))

    if not docs:
        LOGGER.info("No documents to process.")
        return 0

    LOGGER.info("Processing %d documents (dry_run=%s, local_text_scan=%s, submit_form=%s, state=%s)", len(docs), args.dry_run, args.local_text_scan, args.submit_form, args.state)

    stats: dict[str, int] = {"FOUND": 0, "NO_BUTTON": 0, "NOT_FOUND": 0, "FORM_REQUIRED": 0, "ERROR": 0, "SKIPPED": 0}
    examples_found: list[dict[str, Any]] = []
    errors_list: list[dict[str, Any]] = []
    consecutive_errors = 0

    for i, doc in enumerate(docs):
        listing_id = doc.get("listing_id", "?")
        url = doc.get("url", "")
        state = (doc.get("classification") or {}).get("state", "")

        try:
            LOGGER.info("[%d/%d] listing_id=%s state=%s", i + 1, len(docs), listing_id, state)

            if args.submit_form:
                result = process_document_with_form_submission(doc, no_proxy=args.no_proxy)
            elif args.local_text_scan:
                result = process_document_local_scan(doc)
            else:
                result = process_document(
                    doc,
                    dry_run=args.dry_run,
                    use_playwright=args.use_playwright,
                )

            cp = result["contact_phone"]
            status = cp["status"] if cp else "SKIPPED"
            stats[status] = stats.get(status, 0) + 1

            if args.dry_run:
                print(f"\n--- listing_id: {listing_id} ---")
                print(f"  url: {url}")
                print(f"  classification.state: {state}")
                mr = doc.get("manual_review", {})
                if mr:
                    print(f"  manual_review.status: {mr.get('status', 'N/A')}")

                if args.submit_form:
                    print(f"  html_exists: {result.get('html_exists', False)}")
                    fdu = result.get("fake_data_used", {})
                    if fdu:
                        print(f"  fake_data: email={fdu.get('email','')} name={fdu.get('name','')} phone={fdu.get('phone','')} rut={fdu.get('rut','')}")
                elif args.local_text_scan:
                    print(f"  html_exists: {result.get('html_exists', False)}")
                    print(f"  evidence_fields: {result.get('evidence_fields', [])}")
                    if result.get("phone_hits"):
                        print(f"  phone_hits: {len(result['phone_hits'])}")
                        for h in result["phone_hits"][:3]:
                            print(f"    raw={h.get('raw','')} normalized={h.get('normalized','')}")
                else:
                    print(f"  html_exists: {result['html_exists']}")
                    print(f"  has_show_phone: {result['has_show_phone']}")
                    print(f"  has_data_contact: {result['has_data_contact']}")
                    if result.get("data_contact_url"):
                        print(f"  data_contact_url: {result['data_contact_url'][:100]}")
                    if result.get("data_adid"):
                        print(f"  data_adid: {result['data_adid']}")
                    print(f"  has_whatsapp: {result['has_whatsapp']}")
                    print(f"  show_phone_disabled: {result.get('show_phone_disabled', False)}")
                    if result.get("form_indicators"):
                        print(f"  form_indicators: {result['form_indicators']}")

                print(f"  contact_phone -> status: {status}")
                if cp:
                    print(f"  contact_phone.raw: {cp.get('raw', '')}")
                    print(f"  contact_phone.normalized: {cp.get('normalized', '')}")
                    print(f"  contact_phone.last_error: {cp.get('last_error', '')}")
            else:
                if cp and cp.get("status") in ("FOUND", "ERROR"):
                    update_fields = {"contact_phone": cp}
                    update_fields.update(_phone_root_fields(cp))
                    col.update_one(
                        {"listing_id": listing_id},
                        {"$set": update_fields},
                    )
                    LOGGER.info(
                        "  => contact_phone.status=%s raw=%s",
                        status, cp.get("raw", "")[:15],
                    )

            if status == "FOUND" and cp:
                normalized = cp.get("normalized", "")
                masked = normalized[:7] + "****" + normalized[-4:] if len(normalized) > 11 else normalized
                examples_found.append({
                    "listing_id": listing_id,
                    "phone_masked": masked,
                    "field": result.get("evidence_fields", [None])[0] if result.get("evidence_fields") else "",
                })

            if status == "ERROR":
                consecutive_errors += 1
                errors_list.append({
                    "listing_id": listing_id,
                    "last_error": cp.get("last_error", "") if cp else "unknown",
                })
            else:
                consecutive_errors = 0

            if consecutive_errors >= args.max_errors:
                LOGGER.error("Stopping: %d consecutive errors reached", consecutive_errors)
                break

            if i < len(docs) - 1:
                time.sleep(args.sleep)

        except Exception as exc:
            LOGGER.error("Unhandled error for listing_id=%s: %s", listing_id, exc)
            stats["ERROR"] = stats.get("ERROR", 0) + 1
            errors_list.append({"listing_id": listing_id, "last_error": str(exc)})
            consecutive_errors += 1
            if consecutive_errors >= args.max_errors:
                LOGGER.error("Stopping: %d consecutive errors", consecutive_errors)
                break

    print("\n" + "=" * 60)
    print("RESUMEN EXTRACCIÓN TELÉFONOS")
    print("=" * 60)
    print(f"Total candidatos revisados: {len(docs)}")
    print(f"  FOUND:         {stats.get('FOUND', 0)}")
    print(f"  NO_BUTTON:     {stats.get('NO_BUTTON', 0)}")
    print(f"  FORM_REQUIRED: {stats.get('FORM_REQUIRED', 0)}")
    print(f"  NOT_FOUND:     {stats.get('NOT_FOUND', 0)}")
    print(f"  ERROR:         {stats.get('ERROR', 0)}")
    print(f"  SKIPPED:       {stats.get('SKIPPED', 0)}")
    print()

    if examples_found:
        print("Ejemplos FOUND (enmascarados):")
        for ex in examples_found[:10]:
            field = ex.get("field", "")
            fstr = f" [{field}]" if field else ""
            print(f"  listing_id={ex['listing_id']} phone={ex['phone_masked']}{fstr}")

    stats_no_phone = stats.get("SKIPPED", 0)
    if stats_no_phone > 0:
        print(f"\nSin teléfono visible: {stats_no_phone} docs (SKIPPED)")

    if errors_list:
        print("\nErrores:")
        for err in errors_list[:5]:
            print(f"  listing_id={err['listing_id']} error={err['last_error'][:120]}")

    if not args.dry_run:
        print(f"\nModo escritura: contact_phone actualizado en Mongo ({cfg.mongo_db}.{cfg.mongo_collection})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
