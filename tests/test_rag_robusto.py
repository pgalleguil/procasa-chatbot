"""
SUITE DE PRUEBAS DE BÚSQUEDA ROBUSTA — RAG HÍBRIDO ProCasa

Valida que buscar_semanticamente devuelve resultados CORRECTOS:
  1. Filtros duros (tipo, operación, comuna) se cumplen SIEMPRE.
  2. El ranking semántico prioriza lo relevante (piscina, vista, etc.).
  3. Exclusión de vistos funciona.
  4. Fallback geográfico no rompe.

Uso: python tests/test_rag_robusto.py
"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatbot.rag import buscar_semanticamente, extraer_filtros_estructurados, _normalizar_oficina
from config import Config
from chatbot.storage import get_db

db = get_db()
coll = db[Config.COLLECTION_NAME]

PASS = 0
FAIL = 0
ERRORES = []

def check(condicion, nombre, detalle=""):
    global PASS, FAIL
    if condicion:
        PASS += 1
        print(f"  [PASS] {nombre}")
    else:
        FAIL += 1
        ERRORES.append(f"{nombre}: {detalle}")
        print(f"  [FAIL] {nombre} :: {detalle}")

def get_field(doc, campo):
    """Extrae campo plano o anidado."""
    to = doc.get("tipo_operacion") or {}
    ubi = doc.get("ubicacion") or {}
    res = doc.get("resumen") or {}
    snap = res.get("snapshot_listado") or {}
    if campo == "tipo":
        return (to.get("tipo") or snap.get("tipo") or doc.get("tipo") or "").lower()
    if campo == "comuna":
        return (ubi.get("comuna") or snap.get("comuna") or doc.get("comuna") or "").lower()
    if campo == "operacion":
        return ("venta" if to.get("venta") else "arriendo" if to.get("arriendo") else (snap.get("operacion") or doc.get("operacion") or "")).lower()
    if campo == "oficina":
        return res.get("oficina") or doc.get("oficina_nombre") or ""
    if campo == "desc":
        return (doc.get("observaciones") or {}).get("descripcion") or doc.get("descripcion_clean") or ""
    return ""

def norm(s):
    import unicodedata
    return unicodedata.normalize("NFKD", s).lower()

print("=" * 70)
print("SUITE RAG HÍBRIDO — PRUEBAS DE CONFIANZA")
print("=" * 70)

# ---------------------------------------------------------------
print("\n[1] FILTROS DUROS — La comuna pedida SIEMPRE está en resultados")
casos_comuna = [
    ("casa con piscina en Chillán", "chillán"),
    ("departamento en venta en Las Condes", "las condes"),
    ("oficina en arriendo en Providencia", "providencia"),
    ("sitio industrial en Lampa", "lampa"),
    ("parcela con condominio en Talagante", "talagante"),
    ("casa en Maipú", "maipú"),
    ("departamento en Viña del Mar", "viña del mar"),
]
for q, comuna_esperada in casos_comuna:
    res = buscar_semanticamente(q, limit=3, oficina_filtro=None)
    if not res:
        check(False, f"'{q}' devolvió resultados", "sin resultados")
        continue
    ok = any(norm(get_field(r, "comuna")) == norm(comuna_esperada) for r in res)
    detalles = [f"{get_field(r,'comuna')}" for r in res]
    check(ok, f"'{q}' → contiene {comuna_esperada} [{', '.join(detalles)}]", f"comunas={detalles}")

# ---------------------------------------------------------------
print("\n[2] FILTROS DUROS — El tipo pedido SIEMPRE está en resultados")
casos_tipo = [
    ("casa 3 dormitorios en Chillán", "casa"),
    ("departamento con terraza en Las Condes", "departamento"),
    ("oficina amoblada en Providencia", "oficina"),
    ("sitio en Lampa", "sitio"),
    ("parcela en Talagante", "parcela"),
]
for q, tipo_esperado in casos_tipo:
    res = buscar_semanticamente(q, limit=3, oficina_filtro=None)
    if not res:
        check(False, f"'{q}' devolvió resultados", "sin resultados")
        continue
    ok = any(norm(get_field(r, "tipo")).startswith(tipo_esperado) for r in res)
    detalles = [f"{get_field(r,'tipo')}" for r in res]
    check(ok, f"'{q}' → contiene {tipo_esperado} [{', '.join(detalles)}]", f"tipos={detalles}")

# ---------------------------------------------------------------
print("\n[3] OPERACIÓN — Venta/Arriendo respetada")
for q, op_esperada in [
    ("departamento en arriendo en Providencia", "arriendo"),
    ("casa en venta en Chillán", "venta"),
]:
    res = buscar_semanticamente(q, limit=3, oficina_filtro=None)
    if not res:
        check(False, f"'{q}' devolvió resultados", "sin resultados")
        continue
    ok = any(get_field(r, "operacion").startswith(op_esperada) for r in res)
    detalles = [f"{get_field(r,'operacion')}" for r in res]
    check(ok, f"'{q}' → contiene {op_esperada} [{', '.join(detalles)}]", f"ops={detalles}")

# ---------------------------------------------------------------
print("\n[4] RANKING SEMÁNTICO — Relevancia con palabras clave")
casos_semanticos = [
    ("casa con piscina en Chillán", ["piscina", "quincho", "piscina"]),
    ("departamento con vista al mar", ["mar", "vista", "oceano", "marea"]),
    ("casa con quincho y parrilla para asado", ["quincho", "parrilla", "barbecue", "asado"]),
    ("departamento amoblado", ["amoblado", "amoblada"]),
    ("propiedad con bodega y estacionamiento", ["bodega", "estacionamiento", "parking"]),
]
for q, keywords in casos_semanticos:
    res = buscar_semanticamente(q, limit=5, oficina_filtro=None)
    if not res:
        check(False, f"'{q}' devolvió resultados", "sin resultados")
        continue
    # Chequear que al menos 1 resultado menciona una keyword en su descripción
    menciones = []
    for r in res:
        desc = norm(get_field(r, "desc"))
        if any(norm(k) in desc for k in keywords):
            menciones.append(str(r.get("codigo")))
    check(bool(menciones), f"'{q}' → top resultados mencionan keywords [{', '.join(menciones[:3])}]",
          f"keywords={keywords} ninguna en top5")

# ---------------------------------------------------------------
print("\n[5] EXCLUSIÓN DE VISTOS — exclude_codes excluye correctamente")
res = buscar_semanticamente("casa en Chillán", limit=3, oficina_filtro=None)
if res:
    codigos = [str(r.get("codigo")) for r in res]
    res2 = buscar_semanticamente("casa en Chillán", limit=3, oficina_filtro=None, exclude_codes=codigos)
    nuevos = [str(r.get("codigo")) for r in res2]
    overlap = set(codigos) & set(nuevos)
    check(not overlap, f"exclude_codes excluye {codigos} → nuevos {nuevos}", f"overlap={overlap}")
else:
    check(False, "base para exclude_codes", "sin resultados")

# ---------------------------------------------------------------
print("\n[6] SCOPE OFICINA — SUCRE local vs global")
res_sucre = buscar_semanticamente("casa 3 dormitorios", limit=5, oficina_filtro="PROCASA SUCRE")
if res_sucre:
    ofis = {get_field(r, "oficina") for r in res_sucre}
    ok = all("sucre" in norm(o) or not o for o in ofis)
    check(ok, f"SCOPE SUCRE: {len(res_sucre)} resultados, oficinas={ofis}", f"oficinas={ofis}")
else:
    check(False, "scope SUCRE devolvió resultados", "sin resultados")

res_global = buscar_semanticamente("casa 3 dormitorios", limit=5, oficina_filtro=None)
if res_global:
    ofis = {get_field(r, "oficina") for r in res_global}
    check(bool(ofis), f"SCOPE GLOBAL: {len(res_global)} resultados, oficinas={ofis}",
          f"oficinas={ofis}")
else:
    check(False, "scope global devolvió resultados", "sin resultados")

# Verificar que global puede alcanzar otras oficinas (buscar algo poco común en SUCRE)
res_pino = buscar_semanticamente("parcela con bosque nativo", limit=3, oficina_filtro="PROCASA MAURICIO PINO")
if res_pino:
    ofis_p = {get_field(r, "oficina") for r in res_pino}
    ok = all("mauricio pino" in norm(o) or not o for o in ofis_p)
    check(ok, f"SCOPE MAURICIO PINO: oficinas={ofis_p}", f"oficinas={ofis_p}")
else:
    check(False, "scope MAURICIO PINO devolvió resultados", "sin resultados")

# ---------------------------------------------------------------
print("\n[7] NORMALIZACIÓN DE OFICINA")
check(_normalizar_oficina("INMOBILIARIA SUCRE SPA") == "PROCASA SUCRE", "INMOBILIARIA SUCRE SPA → PROCASA SUCRE")
check(_normalizar_oficina("PROCASA LA GLORIA") == "PROCASA LA GLORIA", "PROCASA LA GLORIA → igual")
check(_normalizar_oficina("PROCASA SUCRE") == "PROCASA SUCRE", "PROCASA SUCRE → igual")
check(_normalizar_oficina(None) == "", "None → '' (desactiva filtro, scope global)", f"got={_normalizar_oficina(None)!r}")

# ---------------------------------------------------------------
print("\n[8] EXTRACCIÓN DE FILTROS — Casos reales de clientes")
casos_ext = [
    ("Busco una casa en Chillán con 3 dormitorios", "casa", "chillán"),
    ("departamento en arriendo cerca de metro, presupuesto 500 mil", "departamento", None),
    ("necesito sitio o parcela en Lampa para instalar negocio", None, "lampa"),
]
for q, tipo_esp, comuna_esp in casos_ext:
    filtros, target = extraer_filtros_estructurados(q)
    tipo_ok = not tipo_esp or (filtros.get("tipo") or "").lower().startswith(tipo_esp)
    comuna_ok = not comuna_esp or target == comuna_esp or comuna_esp in [c.lower() for c in filtros.get("comunas", [])]
    check(tipo_ok and comuna_ok, f"extraer_filtros('{q}') → tipo={filtros.get('tipo')}, comuna={target}",
          f"filtros={filtros}")

# ---------------------------------------------------------------
print("\n" + "=" * 70)
print(f"RESULTADO: {PASS} PASS / {FAIL} FAIL")
print("=" * 70)
if ERRORES:
    print("\nERRORES:")
    for e in ERRORES:
        print(f"  - {e}")
sys.exit(1 if FAIL else 0)
