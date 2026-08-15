"""
Discovery completo Toctoc via Playwright + BFF fetch desde contexto navegador.
Cookies en contexto, recursos bloqueados, paginacion via /gw-lista-seo/propiedades.
No modifica scrapers existentes.
"""
import sys, os, json, time, re, hashlib
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
SCT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pymongo import MongoClient
from dotenv import load_dotenv
load_dotenv(dotenv_path=str(ROOT / ".env"))

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "URLS")
CAPTACION_COL = os.getenv("CAPTACION_COLLECTION_NAME", "propiedades_captacion")

COMUNA = "maipu"
COMBOS = [
    ("venta", "departamento"),
    ("venta", "casa"),
    ("arriendo", "departamento"),
    ("arriendo", "casa"),
]
MAX_PAGES = 200


def classify_fmt(url):
    if not url: return "sin_url"
    u = str(url).lower()
    if "/compranuevo/" in u: return "proyecto_nuevo"
    if "/compracorredorasr/" in u: return "corredora"
    if "/arriendocorredorasr/" in u: return "corredora"
    if "/compraparticularsr/" in u: return "particular"
    if "/arriendoparticularsr/" in u: return "particular"
    if "/propiedad/" in u or "/propiedades/" in u: return "particular"
    return "otro"


def extract_lid(url):
    m = re.search(r"/(\d{5,8})(?:[/?#]|$)", url)
    return m.group(1) if m else ""


def build_filtros(operacion, tipo_propiedad, comuna):
    return json.dumps([
        {"id": "tipo-de-busqueda", "value": [
            {"id": "venta" if operacion == "venta" else "arriendo",
             "name": "Venta" if operacion == "venta" else "Arriendo"}]},
        {"id": "operacion", "value": [
            {"id": tipo_propiedad, "name": tipo_propiedad.capitalize()}]},
        {"id": "region", "value": [
            {"id": "metropolitana", "name": "Metropolitana"}]},
        {"id": "comuna", "value": [
            {"id": comuna, "name": comuna.capitalize()}]},
    ])


def fetch_bff_page(page, operacion, tipo, comuna, pagina):
    """Llama al BFF desde dentro del navegador usando fetch()."""
    filtros = build_filtros(operacion, tipo, comuna)
    url = f"https://www.toctoc.com/gw-lista-seo/propiedades?filtros={filtros}&page={pagina}"

    js = """
    async ([url]) => {
        const resp = await fetch(url);
        if (!resp.ok) return {error: resp.status, statusText: resp.statusText};
        return await resp.json();
    }
    """
    try:
        result = page.evaluate(js, [url])
        return result
    except Exception as e:
        return {"error": str(e)}


def extract_items(data, page_num):
    """Extrae items de la respuesta BFF."""
    results = data.get("results", [])
    items = []
    for r in results:
        url_ficha = str(r.get("urlFicha") or r.get("url") or "")
        if not url_ficha: continue
        lid = str(r.get("idProperty") or extract_lid(url_ficha) or "")
        if not lid: continue
        precios = r.get("precios", [])
        price_uf = 0
        if isinstance(precios, list):
            for p in precios:
                if str(p.get("prefix", "")) == "UF":
                    try: price_uf = float(p.get("value", 0))
                    except: pass
        items.append({
            "listing_id": lid,
            "url": url_ficha,
            "format": classify_fmt(url_ficha),
            "comuna": str(r.get("comuna", comuna)),
            "title": str(r.get("titulo", ""))[:100],
            "precio_uf": price_uf,
            "page": page_num,
        })
    return items, data.get("total", 0)


def discover_combo(context, operacion, tipo, comuna):
    """BFF fetch desde contexto navegador con cookies validas."""
    label = f"{operacion}/{tipo}"
    print(f"\n{'='*60}")
    print(f"[{label}]")
    print(f"{'='*60}")

    page = context.new_page()

    def route_handler(route):
        rt = route.request.resource_type
        if rt in ("image", "media", "font"):
            route.abort(); return
        url_l = route.request.url.lower()
        blocked = ("google-analytics", "googletagmanager", "doubleclick",
                   "facebook", "hotjar", "sentry", "tiktok", "hubspot",
                   "linkedin", "trovit", "ipify", "igodigital",
                   "visualwebsiteoptimizer", "hs-scripts", "hsforms",
                   "hscollectedforms", "hsadspixel", "hs-banner",
                   "hs-analytics", "creativecdn")
        if any(d in url_l for d in blocked):
            route.abort(); return
        route.continue_()

    page.route("**/*", route_handler)

    # Cargar pagina para establecer cookies de sesion
    search_url = f"https://www.toctoc.com/{operacion}/{tipo}/metropolitana/{comuna}"
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)
    except Exception as e:
        print(f"  Error carga: {e}")
        page.close()
        return {"label": label, "pages": 0, "items": [], "error": str(e)}

    all_items = []
    seen_ids = set()
    seen_urls = set()
    page_num = 0
    prev_hash = None
    empty_streak = 0
    errors = 0
    total_toctoc = 0
    bff_calls = 0

    while page_num < MAX_PAGES:
        page_num += 1

        # Llamar al BFF desde el navegador
        data = fetch_bff_page(page, operacion, tipo, comuna, page_num)
        bff_calls += 1

        if "error" in data:
            print(f"  Page {page_num}: BFF error={data['error']}")
            errors += 1
            if errors >= 3: break
            time.sleep(1)
            continue

        total = data.get("total", 0)
        results = data.get("results", [])
        bff_page = data.get("page", page_num)
        if not total_toctoc:
            total_toctoc = total

        items, _ = extract_items(data, page_num)
        page_ids = [it["listing_id"] for it in items]

        new_count = 0
        for it in items:
            if it["listing_id"] in seen_ids or it["url"] in seen_urls:
                continue
            seen_ids.add(it["listing_id"])
            seen_urls.add(it["url"])
            all_items.append(it)
            new_count += 1

        this_hash = hashlib.md5(",".join(sorted(page_ids)).encode()).hexdigest()
        print(f"  Page {page_num:2d} (bff_page={bff_page}): total={total}, "
              f"results={len(results)}, new={new_count}, hash={this_hash[:8]}")

        # Detener si pagina vacia
        if len(results) == 0:
            print(f"    Sin resultados")
            break

        # Detener si repite
        if prev_hash and this_hash == prev_hash:
            print(f"    IDs repetidos")
            break
        prev_hash = this_hash

        # Sin nuevos IDs
        if new_count == 0:
            empty_streak += 1
            if empty_streak >= 2:
                print(f"    2 paginas sin nuevos IDs")
                break
        else:
            empty_streak = 0

        # Pausa entre paginas
        time.sleep(0.5)

    page.close()

    return {
        "operacion": operacion, "tipo": tipo, "label": label,
        "pages": page_num, "bff_calls": bff_calls,
        "items": all_items, "total_toctoc": total_toctoc,
        "errors": errors,
    }


def main():
    from playwright.sync_api import sync_playwright

    print("=" * 70)
    print(f"  DISCOVERY TOCTOC BFF — {COMUNA}")
    print(f"  Metodo: Playwright + fetch(/gw-lista-seo/propiedades)")
    print("=" * 70)

    t0 = time.time()

    client = MongoClient(MONGO_URI)
    coll = client[DB_NAME][CAPTACION_COL]
    existing_ids = set()
    for d in coll.find({"origen": "toctoc"}, {"listing_id": 1}):
        if d.get("listing_id"):
            existing_ids.add(str(d["listing_id"]))
    print(f"  IDs existentes MongoDB: {len(existing_ids)}\n")
    client.close()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1920, "height": 1080},
        )

        all_combo_results = []
        all_unique = []
        global_seen_ids = set()

        for op, tp in COMBOS:
            r = discover_combo(context, op, tp, COMUNA)
            all_combo_results.append(r)
            for u in r["items"]:
                if u["listing_id"] in global_seen_ids: continue
                global_seen_ids.add(u["listing_id"])
                u["exists_in_mongo"] = u["listing_id"] in existing_ids
                all_unique.append(u)

        context.close()
        browser.close()

    total_time = time.time() - t0

    # Clasificar
    by_fmt = {"particular": 0, "corredora": 0, "proyecto_nuevo": 0, "otro": 0}
    for u in all_unique:
        by_fmt[u["format"]] += 1

    particulares = [u for u in all_unique if u["format"] == "particular"]
    new_particulares = [u for u in particulares if not u["exists_in_mongo"]]
    conflicts = [u for u in all_unique if u.get("comuna") and
                 u["comuna"].lower().strip() not in ("maipu", "maipú", "")]

    # Reporte
    print(f"\n{'='*70}")
    print(f"  RESULTADOS POR COMBINACION")
    print(f"{'='*70}")
    print(f"  {'Combo':<25} {'Pags':>5} {'BFF':>5} {'URLs':>7} {'Part':>5} {'Corr':>5} {'Nuevo':>5} {'Exis':>5} {'Terr':>5}")
    print(f"  {'-'*25} {'-'*5} {'-'*5} {'-'*7} {'-'*5} {'-'*5} {'-'*5} {'-'*5} {'-'*5}")
    for r in all_combo_results:
        items = r["items"]
        part = sum(1 for u in items if u["format"] == "particular")
        corr = sum(1 for u in items if u["format"] == "corredora")
        nuevo = sum(1 for u in items if u["format"] == "proyecto_nuevo")
        exist = sum(1 for u in items if u["listing_id"] in existing_ids)
        conf = sum(1 for u in items if u.get("comuna") and
                   u["comuna"].lower().strip() not in ("maipu", "maipú", ""))
        print(f"  {r['label']:<25} {r['pages']:>5} {r['bff_calls']:>5} "
              f"{len(items):>7} {part:>5} {corr:>5} {nuevo:>5} {exist:>5} {conf:>5}")

    print(f"\n{'='*70}")
    print(f"  CONSOLIDADO MAIPU")
    print(f"{'='*70}")
    print(f"  Paginas recorridas:         {sum(r['pages'] for r in all_combo_results)}")
    print(f"  BFF calls:                  {sum(r['bff_calls'] for r in all_combo_results)}")
    print(f"  IDs unicos:                 {len(all_unique)}")
    print(f"  Particulares:               {len(particulares)}")
    print(f"  Corredoras:                 {by_fmt['corredora']}")
    print(f"  Proyectos nuevos:           {by_fmt['proyecto_nuevo']}")
    print(f"  Desconocidos:               {by_fmt['otro']}")
    print(f"  Ya existentes MongoDB:      {sum(1 for u in all_unique if u['exists_in_mongo'])}")
    print(f"  Candidatos nuevos:          {len(new_particulares)}")
    print(f"  Conflictos territoriales:   {len(conflicts)}")
    print(f"  Tiempo:                     {total_time:.1f}s")
    print(f"  DeepSeek: 0   Escrituras: 0   Asignaciones: 0   Comunas: 1")

    if conflicts:
        print(f"\n  Conflictos territoriales:")
        for c in conflicts[:10]:
            print(f"    {c['listing_id']} comuna={c['comuna']} url={c['url'][:80]}")

    # ID samples
    for r in all_combo_results:
        items = r["items"]
        if items:
            pages = sorted(set(it["page"] for it in items))
            print(f"\n  {r['label']}: {len(pages)} paginas con datos")
            if len(pages) >= 3:
                for pg in [pages[0], pages[len(pages)//2], pages[-1]]:
                    ids = [it["listing_id"] for it in items if it["page"] == pg][:3]
                    print(f"    Page {pg}: {ids}")
            else:
                for pg in pages:
                    ids = [it["listing_id"] for it in items if it["page"] == pg][:3]
                    print(f"    Page {pg}: {ids}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = str(ROOT / f"reports/toctoc_maipu_bff_{ts}.json")
    (ROOT / "reports").mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "comuna": COMUNA, "method": "playwright+bff",
            "combos": [{k: v for k, v in r.items() if k != "items"} for r in all_combo_results],
            "unique": len(all_unique), "particulares": len(particulares),
            "existing": sum(1 for u in all_unique if u["exists_in_mongo"]),
            "new_particulares": len(new_particulares),
            "duration_s": round(total_time, 1),
        }, f, default=str, indent=2, ensure_ascii=False)
    print(f"\n  Reporte: {report_path}")


if __name__ == "__main__":
    main()
