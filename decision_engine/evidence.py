"""
Motor de Evidencias — Niveles A-E, pesos, confianzas y clasificación.
"""
from dataclasses import dataclass, field
from typing import Optional
import yaml
import os


# ─── Config path ───────────────────────────────────────────────────────────
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "decision_engine.yaml",
)

_EVIDENCE_CACHE = None


def load_config(path: str = None) -> dict:
    """Carga la configuración del motor desde YAML."""
    global _EVIDENCE_CACHE
    p = path or _CONFIG_PATH
    if _EVIDENCE_CACHE is not None and path is None:
        return _EVIDENCE_CACHE
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if path is None:
        _EVIDENCE_CACHE = cfg
    return cfg


# ─── Niveles de evidencia ──────────────────────────────────────────────────
NIVEL_A = "A"  # Certeza absoluta  (0.98-1.00)
NIVEL_B = "B"  # Muy fuerte        (0.85-0.97)
NIVEL_C = "C"  # Moderada          (0.70-0.84)
NIVEL_D = "D"  # Débil             (0.40-0.69)
NIVEL_E = "E"  # Contextual        (sin peso directo)

NIVEL_CONFIANZA = {
    NIVEL_A: 0.98,
    NIVEL_B: 0.90,
    NIVEL_C: 0.78,
    NIVEL_D: 0.55,
    NIVEL_E: 0.0,
}


# ─── Estructura de evidencia ───────────────────────────────────────────────
@dataclass
class Evidencia:
    """Una pieza de evidencia encontrada durante el análisis."""

    tipo: str                     # identificador único de la regla
    nivel: str                    # A, B, C, D, E
    texto_encontrado: str         # fragmento textual
    ubicacion: str                # dónde se encontró (descripcion, footer, jsonld, etc.)
    confianza: float              # 0.0 - 1.0
    peso: int                     # contribución al score
    fragmento_html: str = ""      # fragmento relevante del HTML
    frecuencia: int = 1           # cuántas veces aparece
    valor_asociado: str = ""      # valor extra (teléfono, empresa, etc.)


@dataclass
class ResultadoEtapa:
    """Resultado de una etapa de clasificación."""

    etapa: int
    decision: str                 # "CORREDOR" | "DUEÑO" | "INCIERTO" | "CONTINUAR"
    confianza: float
    evidencias: list[Evidencia] = field(default_factory=list)
    score_acumulado: int = 0
    continuar: bool = True
    razon: str = ""
    detalles: dict = field(default_factory=dict)


# ─── Búsqueda de términos en texto ─────────────────────────────────────────
def buscar_terminos(
    texto: str,
    terminos: list[str],
    modo: str = "contiene",
    patron: str = None,
    excepciones: list[str] = None,
) -> list[str]:
    """
    Busca términos en un texto según el modo especificado.
    Retorna lista de términos encontrados.
    """
    if not texto:
        return []
    texto_lower = texto.lower()
    encontrados = []

    if modo == "contiene":
        for t in terminos:
            t_lower = t.lower()
            if t_lower in texto_lower:
                if excepciones:
                    hay_excepcion = any(e.lower() in texto_lower for e in excepciones)
                    if hay_excepcion:
                        continue
                encontrados.append(t)
    elif modo == "regex" and patron:
        import re

        for m in re.finditer(patron, texto_lower):
            encontrados.append(m.group())
    elif modo == "exacto":
        for t in terminos:
            if t.lower() in [w.lower() for w in texto.split()]:
                if excepciones:
                    hay_excepcion = any(e.lower() in texto_lower for e in excepciones)
                    if hay_excepcion:
                        continue
                encontrados.append(t)

    return encontrados


# ─── Factory de evidencias ─────────────────────────────────────────────────
def crear_evidencia(
    tipo: str,
    nivel: str,
    texto_encontrado: str,
    ubicacion: str,
    peso: int = None,
    fragmento_html: str = "",
    frecuencia: int = 1,
    valor_asociado: str = "",
) -> Evidencia:
    """Crea una evidencia con valores por defecto según nivel."""
    confianza_base = NIVEL_CONFIANZA.get(nivel, 0.5)
    if peso is None:
        # Mapa de pesos por nivel
        pesos = {"A": 10, "B": 7, "C": 4, "D": 2, "E": 0}
        peso = pesos.get(nivel, 1)
    return Evidencia(
        tipo=tipo,
        nivel=nivel,
        texto_encontrado=texto_encontrado,
        ubicacion=ubicacion,
        confianza=confianza_base,
        peso=peso,
        fragmento_html=fragmento_html,
        frecuencia=frecuencia,
        valor_asociado=valor_asociado,
    )


# ─── Config de reglas desde YAML ───────────────────────────────────────────
def get_reglas_etapa1() -> list[dict]:
    """Retorna las reglas configuradas para Etapa 1."""
    cfg = load_config()
    return cfg.get("etapa_1_reglas_absolutas", {}).get("reglas", [])


def get_reglas_etapa3() -> list[dict]:
    """Retorna las reglas configuradas para Etapa 3."""
    cfg = load_config()
    return cfg.get("etapa_3_contenido", {}).get("categorias", [])


def get_umbrales() -> dict:
    """Retorna umbrales de decisión desde configuración."""
    cfg = load_config()
    return cfg.get("umbrales", {})


def get_scoring_config() -> dict:
    """Retorna configuración de scoring."""
    cfg = load_config()
    return cfg.get("scoring", {})


def get_ai_config() -> dict:
    """Retorna configuración de IA."""
    cfg = load_config()
    return cfg.get("etapa_6_ia", {})


def reload_config():
    """Recarga configuración desde disco (útil para testing)."""
    global _EVIDENCE_CACHE
    _EVIDENCE_CACHE = None
    return load_config()
