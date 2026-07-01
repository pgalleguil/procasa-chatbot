"""
Runner — Orquestador del pipeline completo del Decision Engine V2.
Maneja el flujo jerárquico de 6 etapas y genera el Audit Trail.
"""
import hashlib
from typing import Optional

from .evidence import (
    ResultadoEtapa,
    load_config,
    get_umbrales,
    get_scoring_config,
    reload_config,
)
from .audit_trail import (
    generar_audit_trail,
    audit_to_dict,
    audit_compare,
    AuditRecord,
)
from .stage1_absolute import evaluar as evaluar_etapa1


_config_cache = None
_reglas_cache = None


def _get_config():
    global _config_cache
    if _config_cache is None:
        _config_cache = load_config()
    return _config_cache


def _hash_config() -> str:
    """Genera hash de la configuración para trazabilidad."""
    cfg = _get_config()
    raw = str(sorted(cfg.items()))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def classify(publicacion: dict) -> dict:
    """
    Punto de entrada principal.
    Recibe una publicación (dict con todos los campos disponibles)
    y retorna el resultado completo de la clasificación.
    """
    # Inicializar acumuladores
    ruta_arbol = []
    todas_evidencias = []
    score_desglose = {
        "etapa_1_absoluta": 0,
        "etapa_2_perfil": 0,
        "etapa_3_contenido": 0,
        "etapa_4_relaciones": 0,
        "etapa_5_html": 0,
        "etapa_6_ia": None,
        "total": 0,
    }

    config_hash = _hash_config()

    # ─── ETAPA 1: REGLAS ABSOLUTAS ─────────────────────────────────────
    res_etapa1 = evaluar_etapa1(publicacion)
    ruta_arbol.append(_resultado_to_dict(res_etapa1))
    todas_evidencias.extend(res_etapa1.evidencias)

    if not res_etapa1.continuar:
        # CORREDOR por evidencia absoluta
        score_desglose["etapa_1_absoluta"] = 10
        score_desglose["total"] = 10
        return _armar_resultado(
            publicacion=publicacion,
            clasificacion="CORREDOR",
            confianza=res_etapa1.confianza,
            score_total=10,
            score_normalizado=1.0,
            nivel_maximo="A",
            etapa_decision=1,
            ruta_arbol=ruta_arbol,
            evidencias=todas_evidencias,
            score_desglose=score_desglose,
            config_hash=config_hash,
        )

    # ─── SCORE ACUMULADO INICIAL ────────────────────────────────────────
    score_total = 0
    score_total += res_etapa1.score_acumulado
    score_desglose["etapa_1_absoluta"] = res_etapa1.score_acumulado

    # ─── ETAPA 2: PERFIL ────────────────────────────────────────────────
    # (esqueleto — se implementa en Sprint 2)
    res_etapa2 = ResultadoEtapa(
        etapa=2,
        decision="CONTINUAR",
        confianza=0.0,
        score_acumulado=0,
        continuar=True,
        razon="Etapa 2 no implementada (Sprint 2)",
    )
    ruta_arbol.append(_resultado_to_dict(res_etapa2))
    score_total += res_etapa2.score_acumulado
    score_desglose["etapa_2_perfil"] = res_etapa2.score_acumulado

    # ─── ETAPA 3: CONTENIDO ─────────────────────────────────────────────
    # (esqueleto — se implementa en Sprint 2)
    res_etapa3 = ResultadoEtapa(
        etapa=3,
        decision="CONTINUAR",
        confianza=0.0,
        score_acumulado=0,
        continuar=True,
        razon="Etapa 3 no implementada (Sprint 2)",
    )
    ruta_arbol.append(_resultado_to_dict(res_etapa3))
    score_total += res_etapa3.score_acumulado
    score_desglose["etapa_3_contenido"] = res_etapa3.score_acumulado

    # ─── ETAPA 4: RELACIONES ────────────────────────────────────────────
    # (esqueleto — se implementa en Sprint 3)
    res_etapa4 = ResultadoEtapa(
        etapa=4,
        decision="CONTINUAR",
        confianza=0.0,
        score_acumulado=0,
        continuar=True,
        razon="Etapa 4 no implementada (Sprint 3)",
    )
    ruta_arbol.append(_resultado_to_dict(res_etapa4))
    score_total += res_etapa4.score_acumulado
    score_desglose["etapa_4_relaciones"] = res_etapa4.score_acumulado

    # ─── DECISIÓN COMBINADA (umbrales) ──────────────────────────────────
    score_desglose["total"] = score_total
    umbrales = get_umbrales()

    if score_total >= umbrales.get("corredor", 7):
        return _armar_resultado(
            publicacion=publicacion,
            clasificacion="CORREDOR",
            confianza=_calcular_confianza(score_total, 7),
            score_total=score_total,
            score_normalizado=_normalizar(score_total),
            nivel_maximo="C",  # estimado mientras no hay B desde perfil
            etapa_decision=4,
            ruta_arbol=ruta_arbol,
            evidencias=todas_evidencias,
            score_desglose=score_desglose,
            config_hash=config_hash,
        )

    if score_total <= umbrales.get("dueno", 2):
        return _armar_resultado(
            publicacion=publicacion,
            clasificacion="DUEÑO",
            confianza=_calcular_confianza_dueno(score_total),
            score_total=score_total,
            score_normalizado=_normalizar(score_total),
            nivel_maximo="",
            etapa_decision=4,
            ruta_arbol=ruta_arbol,
            evidencias=todas_evidencias,
            score_desglose=score_desglose,
            config_hash=config_hash,
        )

    # ─── ETAPA 5: HTML PROFUNDO ─────────────────────────────────────────
    # (esqueleto — se implementa en Sprint 3)
    res_etapa5 = ResultadoEtapa(
        etapa=5,
        decision="CONTINUAR",
        confianza=0.0,
        score_acumulado=0,
        continuar=True,
        razon="Etapa 5 no implementada (Sprint 3)",
    )
    ruta_arbol.append(_resultado_to_dict(res_etapa5))
    score_total += res_etapa5.score_acumulado
    score_desglose["etapa_5_html"] = res_etapa5.score_acumulado
    score_desglose["total"] = score_total

    # Re-evaluar con score actualizado
    if score_total >= umbrales.get("corredor", 7):
        return _armar_resultado(
            publicacion=publicacion,
            clasificacion="CORREDOR",
            confianza=_calcular_confianza(score_total, 7),
            score_total=score_total,
            score_normalizado=_normalizar(score_total),
            nivel_maximo="C",
            etapa_decision=5,
            ruta_arbol=ruta_arbol,
            evidencias=todas_evidencias,
            score_desglose=score_desglose,
            config_hash=config_hash,
        )

    if score_total <= umbrales.get("dueno", 2):
        return _armar_resultado(
            publicacion=publicacion,
            clasificacion="DUEÑO",
            confianza=_calcular_confianza_dueno(score_total),
            score_total=score_total,
            score_normalizado=_normalizar(score_total),
            nivel_maximo="",
            etapa_decision=5,
            ruta_arbol=ruta_arbol,
            evidencias=todas_evidencias,
            score_desglose=score_desglose,
            config_hash=config_hash,
        )

    # ─── ETAPA 6: IA ────────────────────────────────────────────────────
    # (esqueleto — se implementa en Sprint 4)
    res_etapa6 = ResultadoEtapa(
        etapa=6,
        decision="INCIERTO",
        confianza=0.50,
        score_acumulado=0,
        continuar=False,
        razon="Etapa 6 no implementada (Sprint 4) — clasificando como INCIERTO",
    )
    ruta_arbol.append(_resultado_to_dict(res_etapa6))
    score_desglose["etapa_6_ia"] = 0

    return _armar_resultado(
        publicacion=publicacion,
        clasificacion="INCIERTO",
        confianza=0.50,
        score_total=score_total,
        score_normalizado=_normalizar(score_total),
        nivel_maximo="",
        etapa_decision=6,
        ruta_arbol=ruta_arbol,
        evidencias=todas_evidencias,
        score_desglose=score_desglose,
        config_hash=config_hash,
    )


def classify_v1_compatible(publicacion: dict) -> dict:
    """
    Wrapper de retrocompatibilidad.
    Produce el mismo formato que classify_seller_state() del sistema actual.
    """
    result = classify(publicacion)

    # Mapear estados V2 a V1
    v2_to_v1 = {
        "CORREDOR": "CORREDOR_SEGURO",
        "DUEÑO": "DUEÑO_SEGURO",
        "INCIERTO": "INCIERTO",
    }
    state = v2_to_v1.get(result["clasificacion"], "INCIERTO")

    return {
        "classification_state": state,
        "es_propietario_directo": result["clasificacion"] == "DUEÑO",
        "es_corredor": result["clasificacion"] == "CORREDOR",
        "es_incierto": result["clasificacion"] == "INCIERTO",
        "score_corredor": result["score_total"],
        "score_dueno": 0,
        "motivos_corredor": [
            {"señal": e.get("tipo", ""), "peso": e.get("peso", 0), "motivo": e.get("texto_encontrado", "")}
            for e in result["evidencias"]
        ],
        "motivos_dueno": [],
        "_v2_audit": result.get("audit_trail"),
        "_v2_version": "2.0.0",
    }


def _armar_resultado(
    publicacion: dict,
    clasificacion: str,
    confianza: float,
    score_total: int,
    score_normalizado: float,
    nivel_maximo: str,
    etapa_decision: int,
    ruta_arbol: list,
    evidencias: list,
    score_desglose: dict,
    config_hash: str,
) -> dict:
    """Arma el resultado completo incluyendo Audit Trail."""

    # Generar audit trail
    audit = generar_audit_trail(
        url=publicacion.get("url", ""),
        html=publicacion.get("html_completo", ""),
        publicador=publicacion.get("seller_name") or publicacion.get("publicador", ""),
        company_name=publicacion.get("company_name", ""),
        ruta_arbol=ruta_arbol,
        evidencias=evidencias,
        score_desglose=score_desglose,
        clasificacion=clasificacion,
        confianza=confianza,
        score_total=score_total,
        score_normalizado=score_normalizado,
        nivel_maximo_evidencia=nivel_maximo,
        etapa_decision=etapa_decision,
        version="2.0.0",
        config_hash=config_hash,
    )

    return {
        "clasificacion": clasificacion,
        "confianza": round(confianza, 4),
        "score_total": score_total,
        "score_normalizado": round(score_normalizado, 4),
        "nivel_maximo_evidencia": nivel_maximo,
        "etapa_decision": etapa_decision,
        "ruta_arbol": ruta_arbol,
        "evidencias": [e if isinstance(e, dict) else e.__dict__ for e in evidencias],
        "score_desglose": score_desglose,
        "audit_trail": audit_to_dict(audit),
        "version": "2.0.0",
    }


def _resultado_to_dict(res: ResultadoEtapa) -> dict:
    """Convierte ResultadoEtapa a dict para el audit trail."""
    return {
        "etapa": res.etapa,
        "decision": res.decision,
        "confianza": res.confianza,
        "score": res.score_acumulado,
        "razon": res.razon,
        "detalles": res.detalles,
        "n_evidencias": len(res.evidencias),
    }


def _calcular_confianza(score: int, umbral: int = 7) -> float:
    """Calcula confianza basada en qué tan arriba del umbral está el score."""
    if score >= 10:
        return 0.98
    if score >= 8:
        return 0.95
    if score >= umbral:
        return 0.85
    return 0.75


def _calcular_confianza_dueno(score: int) -> float:
    """Calcula confianza para clasificación DUEÑO."""
    if score <= 0:
        return 0.80
    if score <= 1:
        return 0.75
    if score <= 2:
        return 0.65
    return 0.60


def _normalizar(score: int, maximo: int = 40) -> float:
    """Normaliza score a rango 0.0-1.0."""
    return min(score / maximo, 1.0)
