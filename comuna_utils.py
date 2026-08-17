"""Normalización canónica de comunas compartida por scrapers, distribución y reporte.

Fuente única del slug de comuna. Todas las variantes (Maipu / Maipú / MaipÃº / La
Florida / la-florida / Ñuñoa ...) deben resolver al mismo slug canónico para que la
asignación por comuna no pierda candidatos.
"""
import re
import unicodedata

# Patrones de mojibake: secuencias UTF-8 leídas como latin-1/cp1252.
MOJIBAKE_PAIRS = {
    "Ã¡": "á", "Ã©": "é", "Ã­": "í", "Ã³": "ó", "Ãº": "ú",
    "Ã±": "ñ", "Ã¼": "ü", "Ã¤": "ä", "Ã¶": "ö", "Ã§": "ç",
    "Â°": "°", "Ã": "Ã", "Â": "", "â€“": "–",
}


def fix_mojibake(value: str) -> str:
    """Corrige doble-encoding UTF-8->latin1 (p. ej. 'MaipÃº' -> 'Maipú')."""
    if not value:
        return value
    lowered = value
    if "Ã" in lowered or "â€" in lowered:
        try:
            fixed = lowered.encode("latin-1", errors="strict").decode("utf-8", errors="ignore")
        except (UnicodeEncodeError, UnicodeDecodeError):
            fixed = lowered
        if fixed != lowered:
            lowered = fixed
    for bad, good in MOJIBAKE_PAIRS.items():
        lowered = lowered.replace(bad, good)
    return lowered


def normalize_commune_slug(value):
    """Convierte cualquier variante de comuna a slug canónico minúsculo.

    Ejemplos:
        "Maipú"      -> "maipu"
        "MaipÃº"     -> "maipu"   (mojibake)
        "La Florida" -> "la-florida"
        "Ñuñoa"      -> "nunoa"
        "San José de Maipo" -> "san-jose-de-maipo"
    Devuelve None si el valor está vacío o no normalizable.
    """
    if not value:
        return None
    s = str(value)
    s = fix_mojibake(s)
    # Descomponer acentos (NFKD) y remover diacríticos -> ASCII.
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = s.lower().strip()
    s = s.replace("ñ", "n")
    s = re.sub(r"[^a-z0-9\s_-]", "", s)
    s = re.sub(r"[\s_]+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    return s if s else None


def normalize_commune_canonical(value):
    """Alias retrocompatible de normalize_commune_slug."""
    return normalize_commune_slug(value)


def normalize_toctoc_commune(value=None, *, commune_id=None,
                             structured_label=None, structured=False):
    """Normaliza una comuna Toctoc sin inferir Santiago desde texto libre.

    Toctoc publica la comuna oficial Santiago con id 339 y la ruta ``santiago``.
    Solo una etiqueta territorial estructurada (o ese id) habilita el alias
    comercial ``santiago-centro``. Un texto libre que menciona Santiago sigue
    normalizándose como ``santiago`` y no queda automáticamente asignable.
    """
    label = structured_label if structured_label not in (None, "") else value
    slug = normalize_commune_slug(label)
    if not slug:
        return None
    if structured and str(commune_id or "") == "339" and slug == "santiago":
        return "santiago-centro"
    if structured and slug == "santiago":
        return "santiago-centro"
    return slug
