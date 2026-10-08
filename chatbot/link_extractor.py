# chatbot/link_extractor.py → VERSIÓN CON BÚSQUEDA PRIORIZADA POR PLATAFORMA + codigo_procasa
import re
import logging
from typing import Tuple, Optional
from .storage import get_db
from .utils import safe_int_conversion
from config import Config
from .property_lookup import (
    PROPERTY_COLLECTION_NAME,
    find_property_by_any_identifier,
    find_property_by_international_code,
    lookup_property_link,
    canonical_portal,
    normalize_property_url,
    extract_property_external_id,
    operation_from_property_url,
)

URL_RE = re.compile(r'https?://[^\s<>\]\)"]+', re.IGNORECASE)
logger = logging.getLogger(__name__)


def detectar_plataforma(url: str) -> str:
    return {
        "toctoc": "TocToc",
        "yapo": "Yapo",
        "portal_inmobiliario": "PortalInmobiliario",
        "mercadolibre": "MercadoLibre",
        "procasa": "Procasa",
        "enlaceinmobiliario": "Enlace Inmobiliario",
    }.get(canonical_portal(url), "Otro Portal")


def normalizar_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip().rstrip("/")
    url = re.sub(r"^https?://(www\.)?", "", url, flags=re.IGNORECASE)
    return url.lower()


def extraer_ruta_yapo(url: str) -> str:
    """
    Convierte una URL completa de Yapo a la ruta relativa que suele guardar la BD.
    Ej:
      https://www.yapo.cl/bienes-raices-.../32148052
      -> bienes-raices-.../32148052
    """
    if not url:
        return ""
    limpio = normalizar_url(url)
    if limpio.startswith("yapo.cl/"):
        limpio = limpio[len("yapo.cl/"):]
    return limpio.lstrip("/")


def construir_patron_url(url: str) -> dict:
    """
    Construye un patrón flexible para matchear la URL aunque cambie http/https o www.
    """
    url_norm = normalizar_url(url)
    return {"$regex": rf"^(https?://)?(www\.)?{re.escape(url_norm)}/?$", "$options": "i"}

def extraer_codigo_mercadolibre(url: str) -> Optional[str]:
    # Normalizar para encontrar el código MLC
    match = re.search(r"MLC[-_]?(\d+)", url, re.IGNORECASE)
    if match:
        codigo = f"MLC{match.group(1)}"
        print(f"[EXTRACCION] Codigo MLC detectado -> {codigo}")
        return codigo
    return None

def extraer_codigo_yapo(url: str) -> Optional[str]:
    """
    Extrae el código numérico al final de una URL de Yapo.cl
    Ejemplo: .../28546597 → "28546597"
    """
    match = re.search(r"/(\d{8,12})$", url)
    if match:
        codigo = match.group(1)
        print(f"[EXTRACCION] Codigo Yapo detectado -> {codigo}")
        return codigo
    return None

_INTERNATIONAL_CODE_PATTERNS = (
    re.compile(
        r"\bpropiedad\s*(?:n(?:ro|[úu]m(?:ero)?)?\.?\s*)?[:#-]?\s*(\d{9,10})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bc[oó]digo\s+(?:internacional|(?:de\s+)?(?:la\s+)?propiedad|"
        r"de\s+(?:la\s+)?publicaci[oó]n|de\s+aviso)\s*[:#-]?\s*(\d{9,10})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bidentificador\s+(?:internacional|(?:de\s+)?(?:la\s+)?propiedad)"
        r"\s*[:#-]?\s*(\d{9,10})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bpublicaci[oó]n\s*(?:n(?:ro|[úu]m(?:ero)?)?\.?\s*)?[:#-]?\s*(\d{9,10})\b",
        re.IGNORECASE,
    ),
)


def extraer_codigos_internacionales(mensaje: str) -> list[str]:
    """Extract distinct long property IDs explicitly labeled in message text.

    URLs are excluded so an unknown link's path, phone numbers, RUTs, and prices
    cannot become property identities just because they contain many digits.
    """
    text = URL_RE.sub(" ", str(mensaje or ""))
    found = []
    for pattern in _INTERNATIONAL_CODE_PATTERNS:
        found.extend(match.group(1) for match in pattern.finditer(text))
    return list(dict.fromkeys(found))


def extraer_codigo_internacional(mensaje: str) -> Optional[str]:
    """Return a single explicitly labeled international property ID."""
    codes = extraer_codigos_internacionales(mensaje)
    if len(codes) == 1:
        codigo = codes[0]
        print(f"[EXTRACCION] Codigo Internacional detectado -> {codigo}")
        return codigo
    return None


def extraer_codigo_toctoc_compuesto(mensaje: str) -> Optional[str]:
    """
    Extrae el código interno de propiedad desde el código compuesto de 17 dígitos
    que TocToc incluye en mensajes predefinidos de WhatsApp.
    Ejemplo: 'Código Propiedad: 10543021020006260' -> '6260'
    Los últimos 5 dígitos del código compuesto (sin ceros a la izquierda) son el código interno.
    """
    if not mensaje:
        return None
    match = re.search(r"\b(10543\d{12})\b", mensaje)
    if match:
        codigo_interno = str(int(match.group(1)[-5:]))
        print(f"[EXTRACCION] Codigo compuesto TocToc {match.group(1)} -> interno: {codigo_interno}")
        return codigo_interno
    return None


def analizar_mensaje_para_link(mensaje: str, phone=None, trace_id: str = None) -> Tuple[bool, Optional[dict], str, Optional[str]]:
    """
    Analiza el mensaje buscando URLs y busca la propiedad en la DB.
    Prioriza el campo de búsqueda según la plataforma detectada en la URL.
    Retorna: (encontrado_link, propiedad_encontrada, plataforma_origen, codigo_externo)
    """
    urls = URL_RE.findall(mensaje)
    db = get_db()
    coleccion = db[PROPERTY_COLLECTION_NAME]
    logger.info(f"[LINK_TRACE] trace={trace_id or 'no-trace'} phone={phone} inicio_resolucion_link")
    logger.info(
        f"[LINK_TRACE_META]\ntrace={trace_id or 'no-trace'}\nphone={phone}\n"
        f"collection={PROPERTY_COLLECTION_NAME}\n"
        f"url_count={len(urls)}"
    )
    logger.info(
        f"[LINK_EXTRACT]\ntrace={trace_id or 'no-trace'}\nphone={phone}\n"
        f"mensaje_original={mensaje}\nurls_encontradas={urls}"
    )
    
    for url in urls:
        # Limpieza básica
        url_clean = url.split("?")[0].split("#")[0].rstrip("/")
        url_lower = url_clean.lower()
        url_regex = construir_patron_url(url_clean)
        url_norm = normalizar_url(url_clean)
        
        # === PASO 1: Identificar plataforma ===
        plataforma = detectar_plataforma(url_clean)

        # Aliases: resolver primero por ID externo y luego por URL normalizada.
        alias_prop, alias_meta = lookup_property_link(db, url_clean)
        if alias_prop:
            propiedad = dict(alias_prop)
            propiedad["_link_match"] = alias_meta
            codigo_externo = alias_meta.get("external_id")
            logger.info(
                "[PROPERTY_LINK_MATCH] portal=%s external_id=%s operation=%s property_code=%s match_method=%s",
                alias_meta.get("portal"), alias_meta.get("external_id"), alias_meta.get("operation"),
                propiedad.get("codigo"), alias_meta.get("match_method"),
            )
            return True, propiedad, plataforma, codigo_externo
        if alias_meta.get("error_code") == "AMBIGUOUS_PROPERTY_REFERENCE":
            logger.error(
                "[PROPERTY_LINK_AMBIGUOUS] portal=%s external_id=%s candidate_codes=%s",
                alias_meta.get("portal"), alias_meta.get("external_id"),
                alias_meta.get("candidate_codes", []),
            )
            return False, None, plataforma, alias_meta.get("external_id")
        
        print(f"\n[INFO] Plataforma detectada: {plataforma} | Buscando en {PROPERTY_COLLECTION_NAME}")
        print(f"[LINK_DEBUG] URL recibida: {url_clean}")
        print(f"[LINK_DEBUG] URL normalizada: {url_norm}")
        logger.info(
            f"[LINK_EXTRACT]\ntrace={trace_id or 'no-trace'}\nphone={phone}\n"
            f"url_original={url}\nurl_clean={url_clean}\nrepr_url_clean={repr(url_clean)}\n"
            f"len_url_clean={len(url_clean)}\nplataforma_detectada={plataforma}"
        )
        logger.info(
            f"[LINK_NORMALIZED]\ntrace={trace_id or 'no-trace'}\nphone={phone}\n"
            f"url_lower={url_lower}\nurl_norm={url_norm}\n"
            f"has_www={'www.' in url_clean.lower()}\nhas_https={url_clean.lower().startswith('https://')}\n"
            f"has_http={url_clean.lower().startswith('http://')}"
        )
        
        propiedad = None
        codigo_externo = None
        debug_info = {
            "url_original": url,
            "url_clean": url_clean,
            "repr_url_clean": repr(url_clean),
            "len_url_clean": len(url_clean),
            "platform": plataforma,
        }

        # === PASO 2: RESOLUCIÓN DETERMINÍSTICA POR PLATAFORMA ===
        if plataforma == "Yapo":
            cod_yapo = extraer_codigo_yapo(url_clean)
            ruta_yapo = extraer_ruta_yapo(url_clean)
            if cod_yapo:
                logger.info(f"[LINK_ID]\ntrace={trace_id or 'no-trace'}\nphone={phone}\ncodigo_yapo={cod_yapo}")
            else:
                logger.info(f"[LINK_ID_FAIL]\ntrace={trace_id or 'no-trace'}\nphone={phone}\nurl={url_clean}")
            # La BD guarda url_yapo como URL completa → buscar por URL completa, ruta relativa y código numérico
            query_usada = {"publicaciones.yapo.url_yapo": url_clean}
            logger.info(
                f"[LINK_QUERY]\ntrace={trace_id or 'no-trace'}\nphone={phone}\ncollection={PROPERTY_COLLECTION_NAME}\n"
                f"filtro_exacto={query_usada}"
            )
            logger.info(
                f"[LINK_QUERY_CONTEXT]\ntrace={trace_id or 'no-trace'}\nphone={phone}\n"
                f"codigo_yapo={cod_yapo}\n"
                f"ruta_yapo={ruta_yapo}\n"
                f"url_clean={url_clean}\n"
                f"se_busca_en=publicaciones.yapo.url_yapo (URL completa + ruta + código)"
            )
            prop = coleccion.find_one(query_usada)
            propiedad = prop
            debug_info["exact_match"] = bool(propiedad)
            debug_info["regex_match"] = False
            debug_info["urls_encontradas"] = []
            # Candidatos en orden de especificidad: URL completa → ruta relativa → regex por código
            candidatos = [
                {"publicaciones.yapo.url_yapo": url_clean},                                                          # URL completa exacta (como la guarda el scraper)
                {"publicaciones.yapo.url_yapo": {"$regex": re.escape(url_clean) + r"/?$", "$options": "i"}},        # URL completa con regex
                {"publicaciones.yapo.url_yapo": ruta_yapo},                                                          # Ruta relativa (sin dominio)
                {"publicaciones.yapo.url_yapo": {"$regex": re.escape(ruta_yapo) + r"$", "$options": "i"}},          # Ruta relativa con regex
                {"publicaciones.yapo.url_yapo": {"$regex": re.escape(cod_yapo) + r"$", "$options": "i"}} if cod_yapo else None,  # Solo el código numérico
                {"codigo_yapo": cod_yapo} if cod_yapo else None,                                                     # Campo raíz codigo_yapo
                {"publicaciones.yapo.codigo_yapo": cod_yapo} if cod_yapo else None,                                  # Campo anidado codigo_yapo
                {"yapo.url_yapo": url_clean},                                                                        # Esquema antiguo
                {"url_yapo": url_clean},                                                                              # Esquema plano
            ]
            if not propiedad:
                for i, q in enumerate([c for c in candidatos if c], 1):
                    print(f"[LINK_DEBUG] Yapo query #{i}: {q}")
                    logger.info(
                        f"[LINK_QUERY]\ntrace={trace_id or 'no-trace'}\nphone={phone}\ncollection={PROPERTY_COLLECTION_NAME}\n"
                        f"filtro_exacto={q}"
                    )
                    propiedad = coleccion.find_one(q)
                    if propiedad:
                        print(f"[LINK_DEBUG] Yapo match en query #{i} -> codigo={propiedad.get('codigo')}")
                        logger.info(
                            f"[LINK_QUERY_OK]\ntrace={trace_id or 'no-trace'}\nphone={phone}\n"
                            f"codigo_propiedad={propiedad.get('codigo')}\n"
                            f"codigo_yapo={propiedad.get('codigo_yapo') or propiedad.get('publicaciones', {}).get('yapo', {}).get('codigo_yapo')}\n"
                            f"ejecutivo={propiedad.get('ejecutivo')}\ncomuna={propiedad.get('comuna')}\n"
                            f"yapo_url_db={(propiedad.get('publicaciones', {}).get('yapo', {}).get('url_yapo') or propiedad.get('url_yapo'))}"
                        )
                        break
                if not propiedad:
                    logger.info(
                        f"[LINK_QUERY_FAIL]\ntrace={trace_id or 'no-trace'}\nphone={phone}\n"
                        f"se_probo_query_exacto={url_clean}\n"
                        f"ruta_yapo={ruta_yapo}\n"
                        f"codigo_yapo={cod_yapo}\n"
                        f"collection={PROPERTY_COLLECTION_NAME}\n"
                        f"buscado_en=['publicaciones.yapo.url_yapo']"
                    )
            codigo_externo = cod_yapo
            if phone:
                try:
                    from .storage import actualizar_prospecto
                    debug_info["query_usada"] = query_usada
                    debug_info["resultado"] = "SUCCESS" if propiedad else "NOT_FOUND"
                    debug_info["timestamp"] = __import__("datetime").datetime.now().isoformat()
                    debug_info["collection"] = PROPERTY_COLLECTION_NAME
                    debug_info["ruta_prioritaria"] = "publicaciones.yapo.url_yapo"
                    debug_info["ruta_yapo"] = ruta_yapo
                    actualizar_prospecto(phone, {"debug_link": debug_info}, trace_id)
                except Exception:
                    logger.exception(f"[LINK_EXCEPTION] trace={trace_id or 'no-trace'} phone={phone} tipo_error=debug_persist mensaje=fallo_guardando_debug_link")

        elif plataforma == "Procasa":
            match_pc = re.search(r"/(\d+)$", url_clean)
            cod_path = match_pc.group(1) if match_pc else None
            candidatos = []
            if cod_path:
                candidatos.extend([
                    {"codigo": cod_path},
                    {"codigo": cod_path},
                    {"codigo": safe_int_conversion(cod_path)},
                    {"publicaciones.procasa.url_procasa": url_clean},
                    {"publicaciones.procasa.url_procasa": url_regex},
                ])
            for i, q in enumerate([c for c in candidatos if c], 1):
                print(f"[LINK_DEBUG] Procasa query #{i}: {q}")
                propiedad = coleccion.find_one(q)
                if propiedad:
                    print(f"[LINK_DEBUG] Procasa match en query #{i} -> codigo={propiedad.get('codigo')}")
                    break
            codigo_externo = cod_path

        elif plataforma == "MercadoLibre":
            codigo_ml = extraer_codigo_mercadolibre(url_clean)
            candidatos = [
                {"publicaciones.portal_inmobiliario.url_mercado_libre": url_clean},
                {"publicaciones.portal_inmobiliario.url_mercado_libre": url_regex},
                {"codigo_mercadolibre": codigo_ml} if codigo_ml else None,
                {"publicaciones.portal_inmobiliario.codigo_pi": codigo_ml} if codigo_ml else None,
                {"codigo_pi": codigo_ml} if codigo_ml else None,
            ]
            for i, q in enumerate([c for c in candidatos if c], 1):
                print(f"[LINK_DEBUG] ML query #{i}: {q}")
                propiedad = coleccion.find_one(q)
                if propiedad:
                    print(f"[LINK_DEBUG] ML match en query #{i} -> codigo={propiedad.get('codigo')}")
                    break
            codigo_externo = codigo_ml

        elif plataforma == "PortalInmobiliario":
            codigo_pi = extraer_codigo_mercadolibre(url_clean)
            candidatos = [
                {"publicaciones.portal_inmobiliario.url_pi": url_clean},
                {"publicaciones.portal_inmobiliario.url_pi": url_regex},
                {"publicaciones.portal_inmobiliario.codigo_pi": codigo_pi} if codigo_pi else None,
                {"codigo_pi": codigo_pi} if codigo_pi else None,
                {"codigo_mercadolibre": codigo_pi} if codigo_pi else None,
            ]
            for i, q in enumerate([c for c in candidatos if c], 1):
                print(f"[LINK_DEBUG] PI query #{i}: {q}")
                propiedad = coleccion.find_one(q)
                if propiedad:
                    print(f"[LINK_DEBUG] PI match en query #{i} -> codigo={propiedad.get('codigo')}")
                    break
            codigo_externo = codigo_pi

        elif plataforma == "TocToc":
            match_tt = re.search(r"/([a-f0-9]{32,})", url_lower)
            tt_id = match_tt.group(1) if match_tt else None
            candidatos = [
                {"publicaciones.toctoc.url_toctoc": url_clean},
                {"publicaciones.toctoc.url_toctoc": url_regex},
                {"toctoc.enlace": url_clean},
                {"toctoc.enlace": url_regex},
                {"publicaciones.toctoc.url_toctoc": {"$regex": tt_id}} if tt_id else None,
                {"toctoc.enlace": {"$regex": tt_id}} if tt_id else None,
            ]
            for i, q in enumerate([c for c in candidatos if c], 1):
                print(f"[LINK_DEBUG] TocToc query #{i}: {q}")
                propiedad = coleccion.find_one(q)
                if propiedad:
                    print(f"[LINK_DEBUG] TocToc match en query #{i} -> codigo={propiedad.get('codigo')}")
                    break
            codigo_externo = tt_id

        elif plataforma == "Otro Portal":
            # No hacemos inventos; solo dejamos trazabilidad del link.
            propiedad = None
            codigo_externo = None

        if propiedad:
            legacy_meta = {
                "portal": canonical_portal(url_clean),
                "external_id": codigo_externo or extract_property_external_id(url_clean),
                "operation": operation_from_property_url(url_clean),
                "url_normalized": normalize_property_url(url_clean),
                "match_method": "legacy_field",
            }
            propiedad = dict(propiedad)
            propiedad["_link_match"] = legacy_meta
            codigo_externo = legacy_meta.get("external_id") or codigo_externo
            logger.info(
                "[PROPERTY_LINK_MATCH] portal=%s external_id=%s operation=%s property_code=%s match_method=legacy_field",
                legacy_meta.get("portal"), legacy_meta.get("external_id"), legacy_meta.get("operation"),
                propiedad.get("codigo"),
            )
            print(f"[EXITO] PROPIEDAD ENCONTRADA | Plataforma: {plataforma} | Código Procasa: {propiedad.get('codigo')}")
            logger.info(
                f"[LINK_QUERY_OK]\ntrace={trace_id or 'no-trace'}\nphone={phone}\n"
                f"codigo_propiedad={propiedad.get('codigo')}\n"
                f"codigo_yapo={propiedad.get('codigo_yapo') or propiedad.get('publicaciones', {}).get('yapo', {}).get('codigo_yapo')}\n"
                f"ejecutivo={propiedad.get('ejecutivo')}\ncomuna={propiedad.get('comuna')}"
            )
            logger.info(
                f"[LINK_RESULT]\ntrace={trace_id or 'no-trace'}\nphone={phone}\nstatus=SUCCESS\n"
                f"codigo_propiedad={propiedad.get('codigo')}\n"
                f"origen={plataforma}"
            )
            logger.info(
                f"[LINK_SUMMARY]\ntrace={trace_id or 'no-trace'}\nphone={phone}\nurl_detectada=True\n"
                f"plataforma={plataforma}\ncodigo_yapo={codigo_externo}\nquery_ok=True\n"
                f"propiedad={propiedad.get('codigo')}\nstatus=SUCCESS"
            )
            return True, propiedad, plataforma, codigo_externo
        else:
            print(f"[FALLO] NO se encontró propiedad con el link '{url_clean[:80]}'")
            logger.info(
                f"[LINK_QUERY_EMPTY]\ntrace={trace_id or 'no-trace'}\nphone={phone}\n"
                f"codigo_yapo={codigo_externo}\ncollection={PROPERTY_COLLECTION_NAME}"
            )
            similares = []
            try:
                if codigo_externo:
                    similares = list(coleccion.find(
                        {"$or": [
                            {"codigo_yapo": {"$regex": re.escape(str(codigo_externo)), "$options": "i"}},
                            {"codigo_mercadolibre": {"$regex": re.escape(str(codigo_externo)), "$options": "i"}},
                            {"codigo_internacional": {"$regex": re.escape(str(codigo_externo)), "$options": "i"}},
                            {"publicaciones.codigo_internacional": {"$regex": re.escape(str(codigo_externo)), "$options": "i"}},
                            {"publicaciones.yapo.url_yapo": {"$regex": re.escape(str(codigo_externo)), "$options": "i"}},
                        ]},
                        {"codigo": 1, "codigo_yapo": 1}
                    ).limit(5))
            except Exception:
                logger.exception(f"[LINK_EXCEPTION] trace={trace_id or 'no-trace'} phone={phone} tipo_error=forensics mensaje=fallo_busqueda_similares")
            try:
                from .storage import actualizar_prospecto
                debug_info["query_usada"] = {"$or": [{"yapo.url_yapo": url_clean}, {"publicaciones.yapo.url_yapo": url_clean}]}
                debug_info["resultado"] = "NOT_FOUND"
                debug_info["timestamp"] = __import__("datetime").datetime.now().isoformat()
                debug_info["cantidad_matches_similares"] = len(similares)
                debug_info["matches_similares"] = [str(x.get("codigo")) for x in similares[:5]]
                actualizar_prospecto(phone, {"debug_link": debug_info}, trace_id)
            except Exception:
                logger.exception(f"[LINK_EXCEPTION] trace={trace_id or 'no-trace'} phone={phone} tipo_error=debug_persist mensaje=fallo_guardando_debug_link")
            logger.info(
                f"[LINK_FORENSICS]\ntrace={trace_id or 'no-trace'}\nphone={phone}\n"
                f"cantidad_matches_similares={len(similares)}\n"
                f"codigos={[str(x.get('codigo')) for x in similares[:5]]}"
            )
            logger.info(
                f"[LINK_RESULT]\ntrace={trace_id or 'no-trace'}\nphone={phone}\nstatus=NOT_FOUND\n"
                f"codigo_propiedad=None\norigen={plataforma}"
            )
            logger.info(
                f"[LINK_SUMMARY]\ntrace={trace_id or 'no-trace'}\nphone={phone}\nurl_detectada=True\n"
                f"plataforma={plataforma}\ncodigo_yapo={codigo_externo}\nquery_ok=False\n"
                f"propiedad=None\nstatus=NOT_FOUND"
            )
            return True, None, plataforma, codigo_externo

    return False, None, "", None


def resolver_referencia_propiedad(
    mensaje: str, phone=None, trace_id: str = None,
) -> dict:
    """Resolve one current property reference without guessing its source portal."""
    urls = [u.rstrip(".,;:!?") for u in URL_RE.findall(str(mensaje or ""))]
    explicit_codes = extraer_codigos_internacionales(mensaje)
    platform = detectar_plataforma(urls[0]) if urls else ""
    result = {
        "status": "no_reference",
        "has_url": bool(urls),
        "property": None,
        "platform": platform,
        "external_id": None,
        "explicit_codes": explicit_codes,
        "url": urls[0] if urls else "",
        "error_code": None,
    }
    if not urls and not explicit_codes:
        return result

    db = get_db()
    url_properties: dict[str, dict] = {}
    url_ids = []
    url_ambiguity = None
    for url in urls:
        external_id = extract_property_external_id(url)
        if not external_id:
            continue
        url_ids.append(external_id)
        url_prop, url_meta = lookup_property_link(db, url, PROPERTY_COLLECTION_NAME)
        if url_meta.get("error_code") == "AMBIGUOUS_PROPERTY_REFERENCE":
            url_ambiguity = url_meta
        if url_prop:
            url_properties[str(url_prop.get("codigo") or url_prop.get("codigo_prop360") or "")] = url_prop

    legacy_external_id = None
    if urls:
        # Retain the existing matching paths for all currently supported portals.
        _legacy_found, legacy_prop, legacy_platform, legacy_external_id = (
            analizar_mensaje_para_link(mensaje, phone, trace_id)
        )
        platform = legacy_platform or platform
        if legacy_prop:
            url_properties[str(legacy_prop.get("codigo") or legacy_prop.get("codigo_prop360") or "")] = legacy_prop

    if url_ambiguity:
        result.update(status="ambiguous", error_code="AMBIGUOUS_PROPERTY_REFERENCE")
        result["external_id"] = explicit_codes[0] if len(explicit_codes) == 1 else (url_ids[0] if url_ids else None)
        return result
    if len(url_properties) > 1:
        result.update(status="ambiguous", error_code="AMBIGUOUS_PROPERTY_REFERENCE", platform=platform)
        return result

    url_prop = next(iter(url_properties.values()), None)
    if len(explicit_codes) > 1:
        result.update(status="ambiguous", error_code="AMBIGUOUS_PROPERTY_REFERENCE", platform=platform)
        return result

    if explicit_codes:
        external_id = explicit_codes[0]
        code_prop, code_meta = find_property_by_international_code(
            db, external_id, PROPERTY_COLLECTION_NAME,
        )
        if code_meta.get("error_code") == "AMBIGUOUS_PROPERTY_REFERENCE":
            result.update(
                status="ambiguous", error_code="AMBIGUOUS_PROPERTY_REFERENCE",
                external_id=external_id, platform=platform,
            )
            return result
        if not code_prop:
            result.update(
                status="ambiguous" if url_prop else "unresolved",
                error_code="IDENTIFIER_CONFLICT" if url_prop else code_meta.get("error_code"),
                external_id=external_id,
                platform=platform,
            )
            return result
        code_value = str(code_prop.get("codigo") or code_prop.get("codigo_prop360") or "")
        url_value = str((url_prop or {}).get("codigo") or (url_prop or {}).get("codigo_prop360") or "")
        if url_prop and url_value != code_value:
            result.update(
                status="ambiguous", error_code="IDENTIFIER_CONFLICT",
                external_id=external_id, platform=platform,
            )
            return result
        if not url_prop and url_ids and any(value != external_id for value in url_ids):
            result.update(
                status="ambiguous", error_code="IDENTIFIER_CONFLICT",
                external_id=external_id, platform=platform,
            )
            return result

        prop = url_prop or code_prop
        if urls:
            meta = dict(prop.get("_link_match") or {})
            meta.update({
                "portal": canonical_portal(urls[0]) or "",
                "external_id": legacy_external_id or external_id,
                "operation": operation_from_property_url(urls[0]),
                "url_normalized": normalize_property_url(urls[0]),
                "match_method": meta.get("match_method") or "exact_international_code",
                "identity_source": "message_text" if not url_prop else "url_and_message",
            })
            prop = dict(prop)
            prop["_link_match"] = meta
        result.update(
            status="resolved", property=prop, platform=platform,
            external_id=legacy_external_id or external_id, error_code=None,
        )
        return result

    if url_prop:
        result.update(
            status="resolved", property=url_prop, platform=platform,
            external_id=legacy_external_id or extract_property_external_id(urls[0]),
        )
        return result
    if urls:
        result.update(
            status="unresolved", platform=platform,
            external_id=legacy_external_id or (url_ids[0] if url_ids else None),
            error_code="PROPERTY_NOT_FOUND",
        )
        return result

    external_id = explicit_codes[0]
    code_prop, code_meta = find_property_by_international_code(
        db, external_id, PROPERTY_COLLECTION_NAME,
    )
    if code_meta.get("error_code") == "AMBIGUOUS_PROPERTY_REFERENCE":
        result.update(status="ambiguous", external_id=external_id, error_code="AMBIGUOUS_PROPERTY_REFERENCE")
    elif code_prop:
        result.update(status="resolved", property=code_prop, external_id=external_id)
    else:
        result.update(status="unresolved", external_id=external_id, error_code=code_meta.get("error_code"))
    return result


def extraer_contexto_urls(mensaje: str) -> list[dict]:
    """
    Devuelve un resumen liviano de URLs detectadas para inyectarlo al prompt.
    Sirve para que el modelo no dependa solo de inferir desde texto crudo.
    """
    urls = URL_RE.findall(mensaje)
    contexto = []
    for url in urls:
        url_clean = url.split("?")[0].split("#")[0].rstrip("/")
        plataforma = detectar_plataforma(url_clean)

        item = {"url": url_clean, "plataforma": plataforma}
        ml = extraer_codigo_mercadolibre(url_clean)
        if ml:
            item["codigo_mercadolibre"] = ml
        yapo = extraer_codigo_yapo(url_clean)
        if yapo:
            item["codigo_yapo"] = yapo
        contexto.append(item)
    return contexto

