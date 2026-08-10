"""
SUITE FASE 2 — FILTROS DUROS NUEVOS Y CORRECCIONES DE CONTEXTO (RAG ProCasa)

Cubre (16 casos obligatorios + extras):
  1. Orientación (norte / poniente) como filtro HARD solo con contexto explícito.
  2. Gastos comunes: UF+GC sin contaminar precio, rango GC, "máximo 100 mil".
  3. Bodegas: exacto / mínimo / "una bodega" / sin bodega (0).
  4. Estacionamientos: "dos" / "al menos dos" / sin estacionamiento.
  5. Piso: desde / hasta / exacto (incl. orden invertido "piso desde el 5").
  6. Operación en contexto de inversión: "comprar para arrendarlo" -> Venta,
     "inversión fácil de arrendar" -> SIN Arriendo.
  7. Vecinos desde lenguaje natural ("si no hay, comunas similares").
  8. Cláusulas MongoDB: orientación, GC (con floor anti-anomalía 1000),
     estacionamientos, bodegas (límite <=30 anti 319), piso.

Uso: python tests/test_rag_fase2.py
"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatbot.rag import (
    extraer_filtros_estructurados, _construir_filtros_mongo,
    _regex_orientacion_compatible, _post_filtrar_atributos,
    orientacion_compatible, normalizar_orientacion,
)

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

def verifica(q, esperado):
    f, _ = extraer_filtros_estructurados(q)
    ok = True
    detalles = []
    for k, v in esperado.items():
        got = f.get(k)
        if got != v:
            ok = False
            detalles.append(f"{k}={got!r} (esperaba {v!r})")
    check(ok, f"EXTRACCIÓN: {q[:62]}", "; ".join(detalles))
    return f

print("=" * 78)
print("FASE 2 — EXTRACCIÓN DE FILTROS")
print("=" * 78)

# 1. Orientación HARD
verifica("departamento orientación norte en Ñuñoa", {"orientacion": "Norte"})
verifica("departamento orientación poniente", {"orientacion": "Poniente"})
verifica("departamento soleado en Providencia", {"orientacion": None})

# 2. Gastos comunes (sin contaminar precio)
verifica("departamento hasta 6000 UF y gastos comunes hasta $120.000",
         {"precio_uf_max": 6000, "gastos_comunes_max": 120000, "precio_clp_max": None})
verifica("departamento con gastos comunes entre $80.000 y $130.000",
         {"gastos_comunes_min": 80000, "gastos_comunes_max": 130000})
verifica("departamento con GC máximo 100 mil", {"gastos_comunes_max": 100000})

# 3. Bodegas exacto/mínimo/palabra/negación
verifica("departamento con exactamente 2 bodegas", {"bodegas": 2, "bodegas_exacto": True})
verifica("departamento con al menos 2 bodegas", {"bodegas": 2, "bodegas_exacto": False})
verifica("departamento con una bodega", {"bodegas": 1, "bodegas_exacto": True})
verifica("departamento sin bodega", {"bodegas": 0, "bodegas_exacto": True})

# 4. Estacionamientos exacto/mínimo/palabra/negación
verifica("departamento con dos estacionamientos", {"estacionamientos": 2, "estacionamientos_exacto": True})
verifica("departamento con al menos dos estacionamientos", {"estacionamientos": 2, "estacionamientos_exacto": False})
verifica("departamento con 3 estacionamientos", {"estacionamientos": 3, "estacionamientos_exacto": True})
verifica("departamento sin estacionamiento", {"estacionamientos": 0, "estacionamientos_exacto": True})

# 5. Piso desde/hasta/exacto + orden invertido
verifica("departamento desde el piso 5", {"piso": 5, "piso_dir": "min"})
verifica("departamento hasta el piso 8", {"piso": 8, "piso_dir": "max"})
verifica("departamento en el piso 7", {"piso": 7, "piso_dir": "exacto"})
verifica("departamento piso desde el 5 con terraza", {"piso": 5, "piso_dir": "min"})
verifica("departamento piso hasta el 8", {"piso": 8, "piso_dir": "max"})

# 6. Operación en contexto de inversión
verifica("quiero arrendar departamento en Providencia", {"operacion": "Arriendo"})
verifica("quiero comprar departamento para arrendarlo", {"operacion": "Venta"})
verifica("busco propiedad de inversión fácil de arrendar", {"operacion": "Venta"})
verifica("quiero comprar una casa en La Reina", {"operacion": "Venta"})

# 7. Vecinos desde lenguaje natural
verifica("departamento Ñuñoa; si no hay, comunas similares", {"include_neighbors": True})
f = verifica("departamento Ñuñoa", {})
check(not f.get("include_neighbors"), "VECINOS: por defecto False", str(f))

print()
print("=" * 78)
print("FASE 2 — CLAÚSULAS MONGODB")
print("=" * 78)

f, _ = extraer_filtros_estructurados(
    "departamento 2d orientación norte gastos comunes hasta $120.000, "
    "2 estacionamientos, una bodega, piso 5+")
mq = _construir_filtros_mongo(f)
qs = str(mq)
check("caracteristicas.orientacion" in qs, "CLÁUSULA: orientación", "")
check("tipo_operacion.gastos_comunes" in qs, "CLÁUSULA: gastos comunes", "")
check("caracteristicas.estacionamientos" in qs, "CLÁUSULA: estacionamientos", "")
check("caracteristicas.bodegas" in qs, "CLÁUSULA: bodegas", "")
check("caracteristicas.piso" in qs, "CLÁUSULA: piso", "")
check("1000" in qs, "CLÁUSULA: GC floor 1000 (anti-datos anómalos)", "")

# Guard bodegas=319 (dato anómalo código 6199) → límite <=30
f2, _ = extraer_filtros_estructurados("departamento con 319 bodegas")
mq2 = _construir_filtros_mongo(f2)
qs2 = str(mq2)
check("319" in qs2 and "30" in qs2, "CLÁUSULA: bodegas=319 limitada a <=30", qs2)

print()
print("=" * 78)
print("FASE 2 — POLÍTICA DE ORIENTACIÓN (Mongo = post-filtro)")
print("=" * 78)

# --- Extracción: compuesto explícito (un solo token) se conserva tal cual ---
verifica("departamento orientación Nor-Oriente en Vitacura", {"orientacion": "Nor-Oriente"})

# --- Semántica por componentes: cardinal simple vs compuesto explícito ---
def _doc(orientacion):
    return {"caracteristicas": {"orientacion": orientacion}}

# Norte acepta Norte, Nor-Oriente, Nor-Poniente (nunca Sur/Oriente/Poniente puros).
res_norte = _post_filtrar_atributos(
    [_doc("Norte"), _doc("Nor-Oriente"), _doc("Nor-Poniente"), _doc("Sur"),
     _doc("Oriente"), _doc("Poniente"), _doc("Sur-Oriente")], {"orientacion": "Norte"})
check({d["caracteristicas"]["orientacion"] for d in res_norte} == {"Norte", "Nor-Oriente", "Nor-Poniente"},
      "POST-FILTRO: Norte → {Norte, Nor-Oriente, Nor-Poniente}",
      str([d["caracteristicas"]["orientacion"] for d in res_norte]))

# Compuesto explícito Nor-Oriente SOLO acepta Nor-Oriente (no se amplía).
res_no = _post_filtrar_atributos(
    [_doc("Nor-Oriente"), _doc("Norte"), _doc("Oriente"), _doc("Sur-Oriente"),
     _doc("Nor-Poniente")], {"orientacion": "Nor-Oriente"})
check({d["caracteristicas"]["orientacion"] for d in res_no} == {"Nor-Oriente"},
      "POST-FILTRO: Nor-Oriente → solo {Nor-Oriente}",
      str([d["caracteristicas"]["orientacion"] for d in res_no]))

# Dato anómalo NorPoniente-Sur (6 docs) normaliza a Nor-Poniente → compatible con Norte/Poniente.
res_anom = _post_filtrar_atributos([_doc("NorPoniente-Sur")], {"orientacion": "Norte"})
check(len(res_anom) == 1, "POST-FILTRO: NorPoniente-Sur es compatible con Norte",
      str([d["caracteristicas"]["orientacion"] for d in res_anom]))

# --- COHERENCIA TOTAL: el regex Mongo acepta EXACTAMENTE los mismos valores
#     que el post-filtro, para toda combinación requerida vs candidata. ---
_CANON = ["Norte", "Sur", "Oriente", "Poniente",
          "Nor-Oriente", "Nor-Poniente", "Sur-Oriente", "Sur-Poniente"]
_RAW = _CANON + ["norte", "sur", "este", "oeste",
                 "nor-oriente", "nororiente", "noreste", "nor-este",
                 "nor-poniente", "norponiente", "noroeste", "nor-oeste",
                 "NorPoniente-Sur", "nor-poniente-sur",
                 "sur-oriente", "suroriente", "sureste", "sur-este",
                 "sur-poniente", "surponiente", "suroeste", "sur-oeste"]
_coherencia_ok = True
_det = []
for req in _CANON:
    regex = _regex_orientacion_compatible(req)
    for cand in _RAW:
        esperado = orientacion_compatible(req, normalizar_orientacion(cand))
        real = bool(regex.search(cand)) if regex else False
        if esperado != real:
            _coherencia_ok = False
            _det.append(f"req={req} cand={cand!r}: post={esperado} mongo={real}")
check(_coherencia_ok, "COHERENCIA: regex Mongo == post-filtro (todas las combinaciones)",
      "; ".join(_det[:8]))

# --- La cláusula Mongo embebe el regex compatible (Norte → incluye nor-oriente) ---
f3, _ = extraer_filtros_estructurados("departamento orientación norte")
mq3 = _construir_filtros_mongo(f3)
qs3 = str(mq3)
pat_norte = _regex_orientacion_compatible("Norte")
check("caracteristicas.orientacion" in qs3, "CLÁUSULA: orientación presente en $or", "")
check(pat_norte is not None and repr(pat_norte) in qs3,
      "CLÁUSULA: regex Norte embebido en query Mongo", "")
check("nor" in qs3 and "poniente" in qs3,
      "CLÁUSULA: Norte incluye variantes Nor-Oriente/Nor-Poniente", "")

print()
print("=" * 78)
print(f"RESULTADO: {PASS} PASS / {FAIL} FAIL")
print("=" * 78)
if ERRORES:
    for e in ERRORES:
        print("  -", e)
