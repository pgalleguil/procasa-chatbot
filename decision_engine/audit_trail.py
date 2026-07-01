"""
Audit Trail — Registro completo y trazable de cada clasificación.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
import json
import hashlib


@dataclass
class AuditRecord:
    """
    Registro completo de auditoría para una clasificación.
    Permite reconstruir la decisión completa meses después.
    """

    # Metadatos
    version: str = "2.0.0"
    timestamp: str = ""
    url: str = ""
    html_hash: str = ""
    publicador: str = ""
    company_name: str = ""

    # Resultado final
    clasificacion: str = "DESCONOCIDO"
    confianza: float = 0.0
    score_normalizado: float = 0.0
    score_total: int = 0
    nivel_maximo_evidencia: str = ""
    etapa_decision: int = 0

    # Ruta seguida en el árbol (una entrada por etapa)
    ruta_arbol: list[dict] = field(default_factory=list)

    # Evidencias detalladas
    evidencias: list[dict] = field(default_factory=list)

    # Score desglosado por etapa
    score_desglose: dict = field(default_factory=lambda: {
        "etapa_1_absoluta": 0,
        "etapa_2_perfil": 0,
        "etapa_3_contenido": 0,
        "etapa_4_relaciones": 0,
        "etapa_5_html": 0,
        "etapa_6_ia": None,
        "total": 0,
    })

    # Versión y hash de configuración
    config_hash: str = ""

    # Resumen legible
    resumen: str = ""


def generar_audit_trail(
    url: str,
    html: str = None,
    publicador: str = "",
    company_name: str = "",
    ruta_arbol: list[dict] = None,
    evidencias: list = None,
    score_desglose: dict = None,
    clasificacion: str = "DESCONOCIDO",
    confianza: float = 0.0,
    score_total: int = 0,
    score_normalizado: float = 0.0,
    nivel_maximo_evidencia: str = "",
    etapa_decision: int = 0,
    version: str = "2.0.0",
    config_hash: str = "",
) -> AuditRecord:
    """Construye un AuditRecord completo a partir de los resultados."""
    record = AuditRecord(
        version=version,
        timestamp=datetime.now(timezone.utc).isoformat(),
        url=url,
        html_hash=hashlib.md5((html or "").encode()).hexdigest() if html else "",
        publicador=publicador,
        company_name=company_name,
        clasificacion=clasificacion,
        confianza=round(confianza, 4),
        score_normalizado=round(score_normalizado, 4),
        score_total=score_total,
        nivel_maximo_evidencia=nivel_maximo_evidencia,
        etapa_decision=etapa_decision,
        ruta_arbol=ruta_arbol or [],
        evidencias=[asdict(e) if hasattr(e, "tipo") else e for e in (evidencias or [])],
        score_desglose=score_desglose or {
            "etapa_1_absoluta": 0,
            "etapa_2_perfil": 0,
            "etapa_3_contenido": 0,
            "etapa_4_relaciones": 0,
            "etapa_5_html": 0,
            "etapa_6_ia": None,
            "total": 0,
        },
        config_hash=config_hash,
    )
    record.resumen = _generar_resumen(record)
    return record


def _generar_resumen(record: AuditRecord) -> str:
    """Genera un resumen legible de la decisión."""
    partes = [
        f"Clasificación: {record.clasificacion}",
        f"Confianza: {record.confianza:.2%}",
        f"Etapa de decisión: {record.etapa_decision}",
        f"Score total: {record.score_total}",
    ]
    if record.ruta_arbol:
        ruta = " → ".join(
            f"E{r['etapa']}:{r['decision']}"
            for r in record.ruta_arbol
        )
        partes.append(f"Ruta: {ruta}")
    if record.evidencias:
        n = len(record.evidencias)
        partes.append(f"Evidencias: {n} encontradas")
    return " | ".join(partes)


def audit_to_json(record: AuditRecord) -> str:
    """Serializa el AuditRecord a JSON."""
    return json.dumps(asdict(record), ensure_ascii=False, indent=2)


def audit_to_dict(record: AuditRecord) -> dict:
    """Convierte el AuditRecord a dict para almacenamiento en MongoDB."""
    d = asdict(record)
    d["_tipo"] = "audit_trail_v2"
    return d


def audit_compare(v1_result: dict, v2_record: AuditRecord) -> dict:
    """
    Compara resultado del clasificador V1 vs V2.
    Útil para shadow mode y validación.
    """
    v1_state = v1_result.get("classification_state", "N/A")
    v2_state = v2_record.clasificacion

    cambio = False
    if v1_state != v2_state:
        cambio = True

    return {
        "v1_classification": v1_state,
        "v2_classification": v2_state,
        "v1_score": v1_result.get("score_corredor", 0),
        "v2_score": v2_record.score_total,
        "v2_confianza": v2_record.confianza,
        "hubo_cambio": cambio,
        "tipo_cambio": f"{v1_state} → {v2_state}" if cambio else "sin cambio",
        "v1_motivos": v1_result.get("motivos_corredor", []),
        "v2_evidencias": [e.get("tipo", "") for e in v2_record.evidencias],
    }
