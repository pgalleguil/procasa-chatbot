"""FastAPI router for the internal PROCASA SUCRE owner portal preview."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from chatbot.storage import get_db
from analytics.pricing_intelligence.time_utils import BUSINESS_TZ

from .security import require_internal_preview
from .service import (
    get_owner_portal_property_view,
    select_owner_intelligence_property_code,
    select_preview_property_code,
)

router = APIRouter(tags=["owner-portal-preview"])
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _render(request: Request, view: dict) -> HTMLResponse:
    return _templates.TemplateResponse(
        request,
        "owner_portal_preview.html",
        {"request": request, "view": view},
    )


def _format_owner_number(value: object, decimals: int = 2) -> str:
    try:
        formatted = f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)
    return formatted.replace(",", "X").replace(".", ",").replace("X", ".")


_templates.env.globals["format_number"] = _format_owner_number


def _local_market_narrative(local: dict) -> str:
    """Build at most two descriptive sentences from the communal snapshot."""

    geography = local.get("geography") or "la comuna"
    property_type = str(local.get("property_type") or "propiedades")
    sentences: list[str] = []
    public_uf_m2 = local.get("public_uf_m2")
    if public_uf_m2 is not None:
        sentences.append(
            f"El corte disponible para {property_type.lower()} en {geography} registra "
            f"{_format_owner_number(public_uf_m2)} UF/m² publicados."
        )
    variation = local.get("price_variation_12m_pct")
    if variation is not None:
        sentences.append(
            f"El indicador presenta una variación de {_format_owner_number(variation, 1)}% "
            "respecto del período comparable informado."
        )
    elif local.get("active_listings") is not None:
        sentences.append(
            f"Se observaron {_format_owner_number(local['active_listings'], 0)} publicaciones activas "
            "en el corte disponible."
        )
    return " ".join(sentences[:2])


def _commercial_insight(view: dict) -> str:
    """Combine only verified property activity and market-position facts."""

    inquiries = int(view.get("inquiries_previous_30d") or 0)
    if inquiries == 1:
        first = "En los últimos 30 días se registró 1 consulta vinculada a su propiedad."
    elif inquiries:
        first = f"En los últimos 30 días se registraron {inquiries} consultas vinculadas a su propiedad."
    else:
        first = "En los últimos 30 días no se registraron consultas vinculadas a su propiedad."
    publications = len(view.get("publications") or [])
    position = view.get("positioning") or {}
    comparable_count = int((view.get("comparable_cohort") or {}).get("count") or 0)
    label = str(position.get("label") or "").casefold()
    position_text = (
        "sobre el rango central"
        if "sobre" in label
        else "bajo el rango central"
        if "bajo" in label
        else "dentro del rango central"
        if "dentro" in label
        else None
    )
    if publications and comparable_count and position_text:
        second = (
            f"La propiedad mantiene presencia en {publications} canales verificados y su precio se encuentra "
            f"{position_text} de {comparable_count} publicaciones comparables observadas."
        )
    elif publications:
        second = f"La propiedad mantiene presencia en {publications} canales verificados."
    elif comparable_count and position_text:
        second = f"Su precio se encuentra {position_text} de {comparable_count} publicaciones comparables observadas."
    else:
        second = "La lectura se complementa con el contexto local disponible para su propiedad."
    return f"{first} {second}"


def _diagnosis_payload(view: dict, payload: dict) -> dict:
    """Build a short, deterministic diagnosis from facts already in the DTO."""

    inquiries = int(view.get("inquiries_previous_30d") or 0)
    publications = len(view.get("publications") or [])
    comparable_cohort = view.get("comparable_cohort") or {}
    comparable_count = int(comparable_cohort.get("count") or 0)
    current_price = view.get("current_price_uf")
    delta = payload.get("market_delta_pct")

    if publications >= 5:
        exposure_value, exposure_state, exposure_tone = f"{publications} canales", "Alta", "positive"
    elif publications:
        exposure_value, exposure_state, exposure_tone = f"{publications} canales", "Intermedia", "neutral"
    else:
        exposure_value, exposure_state, exposure_tone = "Sin canales", "Limitada", "attention"

    if inquiries == 0:
        response_value, response_state, response_tone = "0 consultas", "Sin consultas", "attention"
    elif inquiries == 1:
        response_value, response_state, response_tone = "1 consulta", "Señal puntual", "neutral"
    else:
        response_value, response_state, response_tone = f"{inquiries} consultas", "Actividad registrada", "positive"

    if delta is None or comparable_count == 0:
        price_value, price_state, price_tone = "Sin muestra", "Sin comparables", "neutral"
    elif delta > 5:
        price_value, price_state, price_tone = f"{_format_owner_number(delta, 1)}%", "Sobre la mediana", "attention"
    elif delta < -5:
        price_value, price_state, price_tone = f"{_format_owner_number(delta, 1)}%", "Bajo la mediana", "neutral"
    else:
        price_value, price_state, price_tone = f"{_format_owner_number(delta, 1)}%", "En rango central", "positive"

    sentences: list[str] = []
    if inquiries == 1:
        sentences.append("Durante los últimos 30 días se registró 1 consulta vinculada a esta propiedad.")
    elif inquiries:
        sentences.append(f"Durante los últimos 30 días se registraron {inquiries} consultas vinculadas a esta propiedad.")
    else:
        sentences.append("Durante los últimos 30 días no se registraron consultas vinculadas a esta propiedad.")
    if publications:
        sentences.append(f"La publicación mantiene presencia en {publications} canales verificados.")
    if current_price is not None and comparable_count and delta is not None:
        relation = "por encima" if delta > 0 else "por debajo" if delta < 0 else "en línea con"
        sentences.append(
            f"El precio actual de {_format_owner_number(current_price, 0)} UF se encuentra "
            f"{relation} de la mediana de {comparable_count} publicaciones comparables observadas."
        )
    narrative = " ".join(sentences[:3])

    if inquiries == 0:
        conclusion = (
            "La señal principal del corte es la ausencia de consultas registradas; "
            "la propiedad mantiene la exposición verificable que se indica arriba."
            if publications
            else "La señal principal del corte es la ausencia de consultas registradas y de canales verificados."
        )
    elif delta is not None and abs(delta) > 5:
        conclusion = "La señal comercial principal del corte es la posición relativa del precio frente a las publicaciones comparables observadas."
    else:
        conclusion = "La lectura conjunta muestra actividad registrada y una posición de precio dentro de la referencia disponible."

    payload["diagnosis_narrative"] = narrative
    payload["diagnosis_conclusion"] = conclusion
    payload["diagnosis_signals"] = [
        {"label": "Exposición", "value": exposure_value, "state": exposure_state, "tone": exposure_tone, "detail": "Publicaciones verificadas"},
        {"label": "Respuesta", "value": response_value, "state": response_state, "tone": response_tone, "detail": "Últimos 30 días"},
        {"label": "Precio", "value": price_value, "state": price_state, "tone": price_tone, "detail": "Frente a publicaciones comparables"},
    ]
    return payload


def _display_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    for pattern in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:10], pattern)
        except ValueError:
            continue
    return None


def _report_summary_payload(view: dict, payload: dict) -> None:
    """Presentation-only summary, built exclusively from already exposed facts."""

    inquiries = int(view.get("inquiries_previous_30d") or 0)
    publications = list(view.get("publications") or [])
    comparable_count = int((view.get("comparable_cohort") or {}).get("count") or 0)
    position = str(payload.get("position_descriptor") or "")
    items = [
        (
            f"La propiedad recibió {inquiries} consulta vinculada durante los últimos 30 días."
            if inquiries == 1
            else f"La propiedad recibió {inquiries} consultas vinculadas durante los últimos 30 días."
        ),
        (
            f"La publicación se mantiene activa en {len(publications)} canal verificado."
            if len(publications) == 1
            else f"La publicación se mantiene activa en {len(publications)} canales verificados."
        ),
    ]
    if comparable_count and position:
        items.append(f"El precio publicado está {position.lower()} frente a {comparable_count} propiedades similares.")
    else:
        items.append("Aún no contamos con información suficiente para realizar una comparación representativa.")
    payload["performance_summary"] = items[:3]

    as_of = _display_date(view.get("data_updated_at"))
    publication_dates = [
        value
        for publication in publications
        if (value := _display_date(publication.get("published_at"))) is not None
    ]
    if as_of and publication_dates:
        payload["days_published"] = max(0, (as_of - min(publication_dates)).days)
    else:
        payload["days_published"] = None

    cohort = view.get("comparable_cohort") or {}
    price_uf = view.get("current_price_uf")
    market_rows = []
    if price_uf is not None and cohort.get("p25_uf") is not None and cohort.get("p75_uf") is not None:
        market_rows.append({
            "label": "Precio publicado",
            "property": f"{_format_owner_number(price_uf, 0)} UF",
            "comparables": f"{_format_owner_number(cohort['p25_uf'], 0)}–{_format_owner_number(cohort['p75_uf'], 0)} UF",
        })
    if payload.get("property_uf_m2") is not None and cohort.get("median_uf_m2") is not None:
        market_rows.append({
            "label": "Precio por m²",
            "property": f"{_format_owner_number(payload['property_uf_m2'])} UF/m²",
            "comparables": f"{_format_owner_number(cohort['median_uf_m2'])} UF/m²",
        })
    if comparable_count and position:
        market_rows.append({
            "label": "Posición",
            "property": position,
            "comparables": "Rango observado",
        })
    payload["market_comparison_rows"] = market_rows

    payload["recommendation_range"] = (
        f"{_format_owner_number(cohort['p25_uf'], 0)}–{_format_owner_number(cohort['p75_uf'], 0)} UF"
        if cohort.get("p25_uf") is not None and cohort.get("p75_uf") is not None
        else None
    )
    if comparable_count and position:
        payload["recommendation_diagnosis"] = (
            f"La propiedad se encuentra {position.lower()} frente a {comparable_count} publicaciones comparables observadas."
        )
    else:
        payload["recommendation_diagnosis"] = "La comparación de precio requiere una muestra mayor de propiedades similares."
    payload["recommendation_action"] = "Conviene revisar la estrategia comercial junto con su ejecutivo antes de definir próximos pasos."


def _convergent_payload(view: dict) -> dict:
    """Add small presentation-only labels without changing the shared DTO."""

    def _format_metric_number(value: object) -> str:
        try:
            return f"{float(value):,.0f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except (TypeError, ValueError):
            return str(value)

    payload = dict(view)
    portal_logos = {
        "portal_inmobiliario": "/static/portal_logos/portal-inmobiliario.png",
        "mercadolibre": "/static/portal_logos/mercado-libre.png",
        "mercado_libre": "/static/portal_logos/mercado-libre.png",
        "toctoc": "/static/portal_logos/toctoc.png",
        "yapo": "/static/portal_logos/yapo.png",
        "chilepropiedades": "/static/portal_logos/chilepropiedades.png",
        "proppit": "/static/portal_logos/proppit.png",
        "procasa": "/static/logo.png",
    }
    payload["publications"] = [
        {**publication, "logo_path": portal_logos.get(publication.get("portal_id"), "/static/logo.png")}
        for publication in (view.get("publications") or [])
    ]
    position = view.get("positioning") or {}
    price_uf = view.get("current_price_uf")
    p10 = position.get("p10_uf")
    p90 = position.get("p90_uf")
    if price_uf is not None and p10 is not None and p90 is not None and p90 > p10:
        percentile = round(10 + 80 * max(0, min(1, (price_uf - p10) / (p90 - p10))))
        payload["owner_position_rank"] = max(1, min(9, round(percentile / 10)))
    else:
        payload["owner_position_rank"] = None

    local = view.get("local_context") or {}
    built_area = view.get("built_area_m2") or view.get("land_area_m2")
    payload["property_uf_m2"] = (
        round(price_uf / built_area, 2)
        if price_uf is not None and built_area
        else None
    )
    indicators = []
    if local.get("price_variation_12m_pct") is not None:
        indicators.append({"label": "Variación 12 meses", "value": local["price_variation_12m_pct"], "unit": "%"})
    if local.get("active_listings") is not None:
        indicators.append({"label": "Publicaciones observadas", "value": local["active_listings"], "unit": "avisos"})
    payload["visible_market_indicators"] = indicators[:4]
    payload["local_market_narrative"] = _local_market_narrative(local)
    payload["national_context_available"] = len(view.get("national_indicators") or []) >= 2
    payload["comparables_have_dates"] = any(
        example.get("observed_at")
        for example in (view.get("comparable_cohort") or {}).get("examples", [])
        if isinstance(example, dict)
    )
    payload["has_activity_history"] = len(view.get("timeline") or []) > 5
    quality = view.get("data_quality") or {}
    previous = view.get("previous_price") or {}
    payload["has_last_update"] = bool(
        quality.get("price_history_available")
        and view.get("last_price_change_at")
        and (previous.get("uf") is not None or previous.get("clp") is not None)
    )
    if payload["has_last_update"] and price_uf is not None and previous.get("uf") is not None:
        delta = round(price_uf - previous["uf"], 2)
        payload["last_update_delta_uf"] = delta
        payload["last_update_delta_pct"] = round((delta / previous["uf"]) * 100, 1) if previous["uf"] else None
    else:
        payload["last_update_delta_uf"] = None
        payload["last_update_delta_pct"] = None

    label = str(position.get("label") or "")
    if "Sobre" in label:
        payload["position_descriptor"] = "Sobre el rango central"
        payload["position_kpi_label"] = "Posición competitiva"
        payload["position_kpi_detail"] = "Según propiedades similares"
    elif "Bajo" in label:
        payload["position_descriptor"] = "Bajo el rango central"
        payload["position_kpi_label"] = "Posición competitiva"
        payload["position_kpi_detail"] = "Según propiedades similares"
    elif position:
        payload["position_descriptor"] = "Dentro del rango central"
        payload["position_kpi_label"] = "Posición competitiva"
        payload["position_kpi_detail"] = "Según propiedades similares"
    else:
        built_area = view.get("built_area_m2")
        land_area = view.get("land_area_m2")
        if built_area is not None or land_area is not None:
            area = built_area if built_area is not None else land_area
            payload["position_kpi_label"] = "Superficie"
            payload["position_descriptor"] = f"{_format_metric_number(area)} m²"
            payload["position_kpi_detail"] = (
                "Superficie construida" if built_area is not None else "Superficie de terreno"
            )
        else:
            bedrooms = view.get("bedrooms")
            bathrooms = view.get("bathrooms")
            characteristics = []
            if bedrooms is not None:
                characteristics.append(f"{_format_metric_number(bedrooms)}D")
            if bathrooms is not None:
                characteristics.append(f"{_format_metric_number(bathrooms)}B")
            if characteristics:
                payload["position_kpi_label"] = "Características"
                payload["position_descriptor"] = " · ".join(characteristics)
                payload["position_kpi_detail"] = "Dormitorios y baños"
            elif view.get("property_type"):
                payload["position_kpi_label"] = "Tipo de propiedad"
                payload["position_descriptor"] = str(view["property_type"])
                payload["position_kpi_detail"] = "Ficha verificada"
            else:
                payload["position_kpi_label"] = "Código de propiedad"
                payload["position_descriptor"] = str(view.get("property_code") or "—")
                payload["position_kpi_detail"] = "Ficha PROCASA SUCRE"

    median_uf = position.get("median_uf") if position else None
    payload["market_delta_pct"] = (
        round(((price_uf - median_uf) / median_uf) * 100, 1)
        if price_uf is not None and median_uf
        else None
    )

    rank = payload.get("owner_position_rank")
    dynamic_owner_text = payload.get("market_position_owner_text")
    if dynamic_owner_text:
        payload["position_translation"] = dynamic_owner_text
    elif "Sobre" in label:
        payload["position_translation"] = "Su precio publicado se encuentra sobre el rango central de propiedades similares observadas."
    elif "Bajo" in label:
        payload["position_translation"] = "Su precio publicado se encuentra bajo el rango central de propiedades similares observadas."
    elif rank is not None:
        payload["position_translation"] = (
            "Su precio publicado se encuentra alrededor de la zona media de la muestra comparable."
        )
    else:
        payload["position_translation"] = "No hay una lectura posicional suficiente para esta muestra."

    payload["commercial_insight"] = _commercial_insight(view)
    payload["pricing_recommendation"] = (
        dict(view["recommendation"])
        if isinstance(view.get("recommendation"), dict)
        else None
    )
    _diagnosis_payload(view, payload)
    _report_summary_payload(view, payload)
    return payload


@router.get("/owner-portal-preview", response_class=HTMLResponse, include_in_schema=False)
async def owner_portal_preview(
    request: Request,
    operation: str | None = Query(default=None),
    _user=Depends(require_internal_preview),
) -> HTMLResponse:
    db = get_db()
    as_of = datetime.now(BUSINESS_TZ)
    code = await run_in_threadpool(select_preview_property_code, db)
    if not code:
        raise HTTPException(status_code=404, detail="No eligible PROCASA SUCRE property")
    view = await run_in_threadpool(get_owner_portal_property_view, db, code, as_of, operation)
    if view is None:
        raise HTTPException(status_code=404, detail="Property is not available in PROCASA SUCRE scope")
    return _render(request, view.to_dict())


@router.get("/owner-portal-preview/{property_code}", response_class=HTMLResponse, include_in_schema=False)
async def owner_portal_preview_for_property(
    property_code: str,
    request: Request,
    operation: str | None = Query(default=None),
    _user=Depends(require_internal_preview),
) -> HTMLResponse:
    as_of = datetime.now(BUSINESS_TZ)
    view = await run_in_threadpool(get_owner_portal_property_view, get_db(), property_code, as_of, operation)
    if view is None:
        raise HTTPException(status_code=404, detail="Property is not available in PROCASA SUCRE scope")
    return _render(request, view.to_dict())


async def _redirect_owner_portal_concept(property_code: str) -> RedirectResponse:
    """Keep old concept links usable without exposing parallel visual versions."""

    return RedirectResponse(
        url=f"/owner-portal-convergent/{property_code}",
        status_code=307,
    )


@router.get("/owner-portal-concepts/a/{property_code}", include_in_schema=False)
async def owner_portal_concept_a(
    property_code: str,
    _user=Depends(require_internal_preview),
) -> RedirectResponse:
    return await _redirect_owner_portal_concept(property_code)


@router.get("/owner-portal-concepts/b/{property_code}", include_in_schema=False)
async def owner_portal_concept_b(
    property_code: str,
    _user=Depends(require_internal_preview),
) -> RedirectResponse:
    return await _redirect_owner_portal_concept(property_code)


@router.get("/owner-portal-concepts/c/{property_code}", include_in_schema=False)
async def owner_portal_concept_c(
    property_code: str,
    _user=Depends(require_internal_preview),
) -> RedirectResponse:
    return await _redirect_owner_portal_concept(property_code)


async def _owner_portal_convergent(
    request: Request,
    property_code: str,
    operation: str | None = None,
    page_as_of: datetime | None = None,
) -> HTMLResponse:
    page_as_of = page_as_of or datetime.now(BUSINESS_TZ)
    view = await run_in_threadpool(get_owner_portal_property_view, get_db(), property_code, page_as_of, operation)
    if view is None:
        raise HTTPException(status_code=404, detail="Property is not available in PROCASA SUCRE scope")
    return _templates.TemplateResponse(
        request,
        "owner_portal_convergent.html",
        {"request": request, "view": _convergent_payload(view.to_dict())},
    )


@router.get("/owner-portal-convergent", response_class=HTMLResponse, include_in_schema=False)
async def owner_portal_convergent(
    request: Request,
    _user=Depends(require_internal_preview),
) -> HTMLResponse:
    as_of = datetime.now(BUSINESS_TZ)
    db = get_db()
    code = await run_in_threadpool(select_owner_intelligence_property_code, db, as_of)
    if not code:
        raise HTTPException(status_code=404, detail="No eligible SUCRE property with recent verified activity")
    return await _owner_portal_convergent(request, code, page_as_of=as_of)


@router.get("/owner-portal-convergent/{property_code}", response_class=HTMLResponse, include_in_schema=False)
async def owner_portal_convergent_for_property(
    property_code: str,
    request: Request,
    operation: str | None = Query(default=None),
    _user=Depends(require_internal_preview),
) -> HTMLResponse:
    return await _owner_portal_convergent(request, property_code, operation)
