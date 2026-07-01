"""
Shadow Mode — Ejecuta V2 en paralelo con V1 sin modificar decisiones reales.
Útil para validación y recolección de métricas comparativas.
"""
import logging
from typing import Optional

logger = logging.getLogger("shadow_mode")


def ejecutar_shadow(
    publicacion: dict,
    resultado_v1: dict,
) -> dict:
    """
    Ejecuta el Decision Engine V2 en shadow mode.
    No modifica el resultado V1. Solo registra qué habría decidido V2.
    """
    try:
        from .runner import classify

        resultado_v2 = classify(publicacion)

        # Análisis comparativo
        comparacion = _comparar_v1_v2(resultado_v1, resultado_v2)

        # Loggear diferencias
        if comparacion["hubo_cambio"]:
            logger.info(
                f"[SHADOW] Diferencia detectada: "
                f"V1={comparacion['v1_clasificacion']} → "
                f"V2={comparacion['v2_clasificacion']} "
                f"(URL: {publicacion.get('url', 'N/A')[:80]})"
            )

        return {
            "shadow_activo": True,
            "v2_resultado": resultado_v2,
            "comparacion": comparacion,
        }

    except Exception as e:
        logger.error(f"[SHADOW] Error ejecutando V2: {e}")
        return {
            "shadow_activo": False,
            "error": str(e),
            "v2_resultado": None,
            "comparacion": None,
        }


def _comparar_v1_v2(v1: dict, v2: dict) -> dict:
    """Compara resultados de V1 y V2 y retorna diferencias."""
    v1_state = v1.get("classification_state", "N/A")
    v2_state = v2.get("clasificacion", "N/A")

    cambio = v1_state != v2_state

    # Mapear estados V2 a V1 para comparación directa
    v2_to_v1 = {"CORREDOR": "CORREDOR_SEGURO", "DUEÑO": "DUEÑO_SEGURO", "INCIERTO": "INCIERTO"}
    v2_state_v1 = v2_to_v1.get(v2_state, "N/A")

    return {
        "v1_clasificacion": v1_state,
        "v2_clasificacion": v2_state,
        "v2_clasificacion_v1_format": v2_state_v1,
        "v1_score": v1.get("score_corredor", 0),
        "v2_score": v2.get("score_total", 0),
        "v2_confianza": v2.get("confianza", 0),
        "hubo_cambio": cambio or (v1_state != v2_state_v1),
        "tipo_cambio": f"{v1_state} → {v2_state}" if cambio else "sin cambio",
        "v2_etapa_decision": v2.get("etapa_decision"),
        "v2_evidencias": [e.get("tipo", "") for e in v2.get("evidencias", [])],
    }


def obtener_metricas_shadow(resultados_shadow: list[dict]) -> dict:
    """
    Agrega métricas de múltiples ejecuciones shadow.
    Útil para reportes post-auditoría.
    """
    total = len(resultados_shadow)
    if total == 0:
        return {"total": 0, "mensaje": "Sin datos shadow"}

    cambios = sum(1 for r in resultados_shadow if r.get("comparacion", {}).get("hubo_cambio"))
    errores = sum(1 for r in resultados_shadow if not r.get("shadow_activo"))

    # Distribución de clasificaciones V2
    from collections import Counter
    v2_clasificaciones = Counter()
    for r in resultados_shadow:
        v2 = r.get("v2_resultado", {})
        v2_clasificaciones[v2.get("clasificacion", "DESCONOCIDO")] += 1

    # Distribución de cambios
    tipos_cambio = Counter()
    for r in resultados_shadow:
        comp = r.get("comparacion", {})
        if comp.get("hubo_cambio"):
            tipos_cambio[comp.get("tipo_cambio", "desconocido")] += 1

    return {
        "total_ejecuciones": total,
        "cambios_detectados": cambios,
        "tasa_cambio": round(cambios / total * 100, 2) if total > 0 else 0,
        "errores": errores,
        "tasa_error": round(errores / total * 100, 2) if total > 0 else 0,
        "distribucion_v2": dict(v2_clasificaciones),
        "tipos_cambio": dict(tipos_cambio),
    }
