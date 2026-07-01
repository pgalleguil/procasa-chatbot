"""
Etapa 1: Reglas Absolutas (Nivel A).
Si UNA SOLA regla se activa → CORREDOR con confianza >= 0.98.
El proceso termina inmediatamente. No se evalúa más.
"""
import re
from .evidence import (
    ResultadoEtapa,
    Evidencia,
    get_reglas_etapa1,
    buscar_terminos,
    crear_evidencia,
    load_config,
)
from .text_utils import normalize_text, clean_emoji


def evaluar(publicacion: dict) -> ResultadoEtapa:
    """
    Evalúa todas las reglas absolutas de Etapa 1.
    Retorna CORREDOR con la primera evidencia encontrada.
    """
    reglas = get_reglas_etapa1()
    evidencias = []

    # Preparar textos de búsqueda
    textos = _preparar_textos(publicacion)

    for regla in reglas:
        if not regla.get("activa", True):
            continue

        resultado = _aplicar_regla(regla, publicacion, textos)
        if resultado:
            evidencias.append(resultado)
            return ResultadoEtapa(
                etapa=1,
                decision="CORREDOR",
                confianza=resultado.confianza,
                evidencias=evidencias,
                score_acumulado=10,
                continuar=False,
                razon=f"Evidencia absoluta: {resultado.tipo}",
                detalles={"regla_activada": resultado.tipo, "texto": resultado.texto_encontrado},
            )

    return ResultadoEtapa(
        etapa=1,
        decision="CONTINUAR",
        confianza=0.0,
        evidencias=[],
        score_acumulado=0,
        continuar=True,
        razon="No se encontró evidencia absoluta de corredor",
    )


def _preparar_textos(publicacion: dict) -> dict:
    """Prepara los textos de búsqueda desde la publicación."""
    textos = {}

    campos_texto = {
        "publicador": publicacion.get("seller_name") or publicacion.get("publicador") or "",
        "company_name": publicacion.get("company_name") or "",
        "descripcion": publicacion.get("description") or publicacion.get("raw_desc") or "",
        "broker_brand": publicacion.get("broker_brand") or "",
        "titulo": publicacion.get("titulo") or publicacion.get("title") or "",
        "texto_html": publicacion.get("texto_html_completo") or publicacion.get("html_text") or "",
        "logo_alt": publicacion.get("logo_alt") or "",
    }

    for campo, texto in campos_texto.items():
        limpio = clean_emoji(normalize_text(str(texto), preserve_utf8=True))
        textos[campo] = limpio
        textos[f"{campo}_raw"] = str(texto)

    # Boolean flags
    textos["seller_is_pro"] = str(publicacion.get("seller_is_pro", False)).lower()

    # Footer extraído del HTML
    textos["footer"] = _extraer_footer(publicacion.get("html_completo", ""))

    # Texto completo combinado para búsqueda general
    textos["full_text"] = " ".join(
        v for k, v in textos.items()
        if not k.endswith("_raw") and k != "footer" and isinstance(v, str)
    )

    return textos


def _extraer_footer(html: str) -> str:
    """Extrae el footer del HTML si está disponible."""
    if not html:
        return ""
    # Buscar footer o última sección de la descripción
    match = re.search(
        r"<footer[^>]*>(.*?)</footer>",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        return clean_emoji(normalize_text(match.group(1), preserve_utf8=True))
    return ""


def _aplicar_regla(regla: dict, publicacion: dict, textos: dict) -> Evidencia | None:
    """Aplica una regla individual. Retorna Evidencia si se activa."""
    tipo = regla.get("tipo", "")
    nivel = regla.get("nivel", "A")
    peso = regla.get("peso", 10)
    confianza = regla.get("confianza", 0.98)
    modo = regla.get("modo", "contiene")
    fuentes = regla.get("fuentes", [])
    terminos = regla.get("terminos", [])
    marcas = regla.get("marcas", [])
    patron = regla.get("patron", "")

    # Elegir qué buscar
    palabras = terminos or marcas

    for fuente in fuentes:
        texto = textos.get(fuente)
        if not texto:
            continue

        # Verificación especial para badge profesional
        if tipo == "badge_profesional":
            if _verificar_badge_pro(publicacion):
                return crear_evidencia(
                    tipo=tipo,
                    nivel=nivel,
                    texto_encontrado="Badge Profesional verificado en HTML",
                    ubicacion=fuente,
                    peso=peso,
                    fragmento_html=publicacion.get("html_completo", "")[:500],
                )
            continue

        # Verificación especial para logo corporativo
        if tipo == "logo_corporativo":
            resultado = _verificar_logo(publicacion, marcas, textos)
            if resultado:
                return resultado
            continue

        # Búsqueda general de términos
        encontrados = buscar_terminos(
            texto=texto,
            terminos=palabras,
            modo=modo,
            patron=patron,
            excepciones=regla.get("excepciones"),
        )
        if encontrados:
            # Verificar contexto de negación
            if _tiene_negacion(texto, encontrados):
                continue
            return crear_evidencia(
                tipo=tipo,
                nivel=nivel,
                texto_encontrado=encontrados[0],
                ubicacion=fuente,
                peso=peso,
                frecuencia=len(encontrados),
            )

    return None


def _verificar_badge_pro(publicacion: dict) -> bool:
    """Verifica que el badge profesional sea real (visible en HTML)."""
    seller_is_pro = publicacion.get("seller_is_pro", False)
    if not seller_is_pro:
        return False
    html = publicacion.get("html_completo", "")
    if not html:
        return seller_is_pro  # confiar en el flag si no hay HTML
    # Buscar indicios visuales del badge
    patrones_badge = [
        r'class="[^"]*badge[^"]*profesional[^"]*"',
        r'class="[^"]*profesional[^"]*badge[^"]*"',
        r"badge[-_]profesional",
        r"profesional[-_]badge",
        r"Profesional\s*Inmobiliario",
    ]
    return any(re.search(p, html, re.IGNORECASE) for p in patrones_badge)


def _verificar_logo(publicacion: dict, marcas: list[str], textos: dict) -> Evidencia | None:
    """Verifica logo corporativo en alt text o src de imágenes."""
    logo_alt = textos.get("logo_alt", "")
    html = publicacion.get("html_completo", "")

    if logo_alt:
        encontrada = buscar_terminos(logo_alt, marcas, modo="contiene")
        if encontrada:
            return crear_evidencia(
                tipo="logo_corporativo",
                nivel="A",
                texto_encontrado=encontrada[0],
                ubicacion="logo_alt",
                peso=10,
            )

    # Buscar en imágenes del HTML
    if html:
        for marca in marcas:
            patron = re.escape(marca)
            if re.search(
                r'<img[^>]*alt=["\']([^"\']*' + patron + r'[^"\']*)["\']',
                html,
                re.IGNORECASE,
            ):
                return crear_evidencia(
                    tipo="logo_corporativo",
                    nivel="A",
                    texto_encontrado=marca,
                    ubicacion="html_img_alt",
                    peso=10,
                    fragmento_html=html[:500],
                )

    return None


# Palabras de negación en español (contexto que invierte el significado)
_PALABRAS_NEGACION = {
    "no", "sin", "nunca", "jamás", "tampoco", "nadie", "nada",
    "excepto", "menos", "salvo", "evitar", "prohibido",
}


def _tiene_negacion(texto: str, terminos_encontrados: list[str]) -> bool:
    """
    Detecta si un término está negado en el contexto.
    Ej: "no corredor", "sin inmobiliaria", "evitar corredores"
    """
    if not texto or not terminos_encontrados:
        return False
    texto_lower = texto.lower()
    for termino in terminos_encontrados:
        t_lower = termino.lower()
        for neg in _PALABRAS_NEGACION:
            # Buscar "no X" o "sin X" cerca del término
            patron = rf"\b{re.escape(neg)}\s+(?:\w+\s+){{0,3}}{re.escape(t_lower)}"
            if re.search(patron, texto_lower, re.IGNORECASE):
                return True
    return False
