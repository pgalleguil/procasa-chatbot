"""Read-only live source builder for the fixed owner-campaign E2E cases.

The only writes in this test harness belong to the existing test ledger and
conversation event store. This module only reads Mongo and public listing image
bytes; it never writes campaign or operational property data.
"""

from __future__ import annotations

import html as html_lib
import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .owner_campaign_test_sender import OwnerCampaignTestCase


PROPERTY_COLLECTION = "universo_cartera_prop360"
APPRAISAL_COLLECTION = "tasaciones"
COMMUNAL_COLLECTION = "mercado_comunal"
PROPERTY_CODES = {"A": "5641", "B": "16521", "C": "16486", "D": "16527"}
EXPLICITLY_EXCLUDED_CODES = frozenset({"6754"})
APPROVED_A_RAW_TARGET_UF = 2857.0
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PLACEHOLDER_EMAIL_LOCALPARTS = frozenset({
    "incorrecto", "correo-incorrecto", "correo_incorrecto", "email-invalido",
    "email_invalido", "sincorreo", "sin-correo", "sin_correo", "noemail", "no-email",
})
IMAGE_RE = re.compile(r"https?://[^\"'<>\s]+(?:\.jpe?g|\.png|\.webp)(?:\?[^\"'<>\s]+)?", re.IGNORECASE)
UNKNOWN_EXECUTIVES = {"", "desconocido", "sin asignar", "no asignado", "ejecutivo", "equipo procasa", "procasa"}
CAMPAIGN_SEGMENTS = {
    "STRONG_PRICE_ADJUSTMENT", "MIXED_EVIDENCE", "COMPETITIVE_LOW_RESPONSE", "INSUFFICIENT_EVIDENCE"
}
PORTALS = ("PortalInmobiliario", "MercadoLibre", "Yapo", "TocToc", "ChilePropiedades", "Proppit", "WhatsApp/Directo", "Otro")
VENTA = "VENTA"
ARRIENDO = "ARRIENDO"
VENTA_ARRIENDO = "VENTA_ARRIENDO"
UNKNOWN_OPERATION = "UNKNOWN"
_SINGULAR_OPERATIONS = {VENTA, ARRIENDO}


class LiveTestCaseBuildError(ValueError):
    """Live records do not safely satisfy a fixed test-case contract."""


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).strip().replace("\u00a0", " ").replace(" ", "")
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        result = float(text)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _path(document: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    current: Any = document
    for part in dotted.split("."):
        if not isinstance(current, Mapping):
            return default
        current = current.get(part)
    return default if current is None else current


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _operation_flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        value = value.strip().casefold()
        if value in {"true", "1", "si", "sí", "yes", "activo", "activa"}:
            return True
        if value in {"false", "0", "no", "inactivo", "inactiva"}:
            return False
    return None


def _operations_in_label(value: Any) -> set[str]:
    text = f" {_fold(value)} "
    operations: set[str] = set()
    if re.search(r"\bventa\b|\bvender\b|\bvendida?\b", text):
        operations.add(VENTA)
    if re.search(r"\barriendo\b|\barrendamiento\b|\balquiler\b|\brenta\b", text):
        operations.add(ARRIENDO)
    return operations


def _operation_result(operations: set[str]) -> str:
    if operations == {VENTA}:
        return VENTA
    if operations == {ARRIENDO}:
        return ARRIENDO
    if operations == _SINGULAR_OPERATIONS:
        return VENTA_ARRIENDO
    return UNKNOWN_OPERATION


def resolve_property_operation(doc: Mapping[str, Any] | Any, *, requested_operation: Any = None) -> str:
    """Resolve operation from canonical flags, then observed listing metadata."""
    if isinstance(doc, str):
        resolved = _operation_result(_operations_in_label(doc))
    elif isinstance(doc, Mapping):
        operation = doc.get("tipo_operacion")
        operation = operation if isinstance(operation, Mapping) else {}
        sale_flag = _operation_flag(operation.get("venta"))
        rent_flag = _operation_flag(operation.get("arriendo"))
        if sale_flag is True or rent_flag is True:
            active = set()
            if sale_flag is True:
                active.add(VENTA)
            if rent_flag is True:
                active.add(ARRIENDO)
            resolved = _operation_result(active)
        elif sale_flag is False and rent_flag is False:
            resolved = UNKNOWN_OPERATION
        else:
            summary = doc.get("resumen")
            summary = summary if isinstance(summary, Mapping) else {}
            listing = summary.get("snapshot_listado")
            listing = listing if isinstance(listing, Mapping) else {}
            labels = [listing.get("operacion")]
            labels.extend((operation.get("operacion"), doc.get("operacion"), summary.get("operacion")))
            state = doc.get("estado")
            if isinstance(state, Mapping):
                labels.append(state.get("operacion"))
            resolved = UNKNOWN_OPERATION
            for label in labels:
                found = _operations_in_label(label)
                if found:
                    resolved = _operation_result(found)
                    break
    else:
        resolved = UNKNOWN_OPERATION
    if requested_operation is not None:
        requested = _operation_result(_operations_in_label(requested_operation))
        if requested not in _SINGULAR_OPERATIONS:
            return UNKNOWN_OPERATION
        if resolved == VENTA_ARRIENDO:
            return requested
        if resolved != requested:
            return UNKNOWN_OPERATION
    return resolved


def operation_price_block(doc: Mapping[str, Any], *, requested_operation: Any = None) -> Mapping[str, Any] | None:
    """Return only the live price block for one unambiguous operation."""
    resolved = resolve_property_operation(doc, requested_operation=requested_operation)
    if resolved not in _SINGULAR_OPERATIONS:
        return None
    operation = doc.get("tipo_operacion")
    if not isinstance(operation, Mapping):
        return None
    block = operation.get("precio_venta" if resolved == VENTA else "precio_arriendo")
    return block if isinstance(block, Mapping) else None


def _variants(code: str) -> list[Any]:
    return [code, int(code)] if code.isdigit() else [code]


def _email_from_property(master: Mapping[str, Any], *, required: bool = True) -> str:
    for value in (
        master.get("email_propietario"), _path(master, "propietario.email"),
        _path(master, "owner.email"), _path(master, "contacto.email"),
        _path(master, "resumen.email_propietario"), master.get("email"),
    ):
        email = str(value or "").strip().casefold()
        localpart = email.partition("@")[0]
        if (
            EMAIL_RE.fullmatch(email)
            and email not in {"incorrecto@procasa.cl", "firmasjpc@gmail.com"}
            and localpart not in PLACEHOLDER_EMAIL_LOCALPARTS
        ):
            return email
    if required:
        raise LiveTestCaseBuildError("owner_email_unavailable")
    return ""


def _active_available(master: Mapping[str, Any]) -> bool:
    state = master.get("estado") if isinstance(master.get("estado"), Mapping) else {}
    return str(state.get("estado_prop360") or "").strip().casefold() == "activa" and master.get("disponible_prop360") is True


def _is_sucre(master: Mapping[str, Any]) -> bool:
    return any(
        _fold(value) == "procasa sucre"
        for value in (
            _path(master, "estado.oficina"), _path(master, "resumen.oficina"), master.get("oficina_nombre")
        )
    )


def _property_type(master: Mapping[str, Any]) -> str:
    raw = str(
        _path(master, "metadata.tipo_propiedad") or _path(master, "resumen.tipo_propiedad")
        or master.get("tipo_propiedad") or master.get("tipo") or ""
    ).strip()
    if not raw:
        raise LiveTestCaseBuildError("property_type_unavailable")
    return raw


def _commune(master: Mapping[str, Any]) -> str:
    value = str(_path(master, "ubicacion.comuna") or master.get("comuna") or "").strip()
    if not value:
        raise LiveTestCaseBuildError("commune_unavailable")
    return value


def _dimensions(master: Mapping[str, Any]) -> dict[str, float | None]:
    return {
        "built": _number(
            _path(master, "caracteristicas.superficie_construida")
            or _path(master, "caracteristicas.m2_construidos")
            or _path(master, "caracteristicas.superficie_util")
        ),
        "land": _number(
            _path(master, "caracteristicas.superficie_terreno")
            or _path(master, "caracteristicas.superficie_terreno_m2")
            or _path(master, "caracteristicas.m2_totales")
        ),
        "bedrooms": _number(_path(master, "caracteristicas.dormitorios") or _path(master, "caracteristicas.habitaciones")),
        "bathrooms": _number(_path(master, "caracteristicas.banos") or _path(master, "caracteristicas.baños")),
        "parking": _number(_path(master, "caracteristicas.estacionamientos")),
    }


def _resolve_executive(db: Any, master: Mapping[str, Any]) -> dict[str, str]:
    name = str(_path(master, "estado.ejecutivo") or "").strip()
    if _fold(name) in UNKNOWN_EXECUTIVES or re.search(r"\b(vacante|pendiente)\b", _fold(name)):
        raise LiveTestCaseBuildError("executive_name_unresolved")
    wanted = _fold(name)
    matches = []
    for user in db["usuarios"].find({}, {
        "_id": 0, "nombre": 1, "telefono": 1, "celular": 1, "phone": 1, "movil": 1,
        "email": 1, "correo": 1, "mail": 1, "rol": 1, "is_active": 1,
    }):
        if _fold(user.get("nombre")) == wanted:
            matches.append(user)
    active_agents = [
        user for user in matches
        if user.get("is_active") is True and str(user.get("rol") or "").strip().casefold() == "agente"
    ]
    if len(active_agents) == 1:
        user = active_agents[0]
    elif len(active_agents) > 1 or len(matches) != 1:
        raise LiveTestCaseBuildError("executive_user_match_not_unique")
    else:
        user = matches[0]
    email = str(user.get("email") or user.get("correo") or user.get("mail") or "").strip().casefold()
    phone = str(user.get("telefono") or user.get("celular") or user.get("phone") or user.get("movil") or "").strip()
    if not EMAIL_RE.fullmatch(email) or len(re.sub(r"\D", "", phone)) < 8:
        raise LiveTestCaseBuildError("executive_contact_unresolved")
    return {"name": name, "email": email, "phone": phone, "initials": ""}


def _stored_image_urls(value: Any, key_hint: str = "", depth: int = 0) -> list[str]:
    if depth > 5:
        return []
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            found.extend(_stored_image_urls(child, f"{key_hint}.{key}" if key_hint else str(key), depth + 1))
    elif isinstance(value, list):
        for child in value:
            found.extend(_stored_image_urls(child, key_hint, depth + 1))
    elif isinstance(value, str):
        text = value.strip()
        hint = key_hint.casefold()
        disallowed = ("avatar", "logo", "ejecutivo", "agent", "seller", "map", "mapa", "icon", "placeholder", "stock")
        if text.startswith(("http://", "https://")) and any(
            token in hint for token in ("image", "imagen", "foto", "photo", "portada", "thumbnail")
        ) and not any(token in hint for token in disallowed):
            found.append(text)
    return found


def _valid_image(url: str) -> bool:
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PROCASA-preview/2.0)", "Range": "bytes=0-31"})
        with urlopen(request, timeout=15) as response:
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
            signature = response.read(32)
        return content_type.startswith("image/") and (
            signature.startswith(b"\xff\xd8\xff") or signature.startswith(b"\x89PNG\r\n\x1a\n")
            or (signature.startswith(b"RIFF") and signature[8:12] == b"WEBP")
        )
    except Exception:
        return False


def _resolve_image(master: Mapping[str, Any], code: str) -> dict[str, Any]:
    stored = list(dict.fromkeys(_stored_image_urls(master)))
    for url in stored:
        if _valid_image(url):
            return {"available": True, "url": url, "source": "UNIVERSO_CARTERA", "count": len(stored)}
    publications = master.get("publicaciones") if isinstance(master.get("publicaciones"), Mapping) else {}
    procasa = publications.get("procasa") if isinstance(publications.get("procasa"), Mapping) else {}
    listing_url = str(procasa.get("url_procasa") or procasa.get("url_procasa_arriendo") or f"https://www.procasa.cl/{code}")
    try:
        if urlsplit(listing_url).hostname not in {"procasa.cl", "www.procasa.cl"}:
            raise ValueError("non_official_listing")
        request = Request(listing_url, headers={"User-Agent": "Mozilla/5.0 (compatible; PROCASA-preview/2.0)"})
        with urlopen(request, timeout=20) as response:
            body = response.read(2_000_000).decode("utf-8", errors="ignore")
        urls = [html_lib.unescape(url) for url in IMAGE_RE.findall(body)]
        urls = list(dict.fromkeys(url for url in urls if re.match(
            rf"^{re.escape(code)}_[^/]+\.(?:jpe?g|png|webp)$", urlsplit(url).path.rsplit("/", 1)[-1], re.IGNORECASE
        )))
        for url in urls:
            if _valid_image(url):
                return {"available": True, "url": url, "source": "PROCASA_PUBLICATION", "count": len(urls)}
    except Exception:
        pass
    raise LiveTestCaseBuildError("property_image_unresolved")


def _own_listing_ids(master: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    def visit(value: Any, depth: int = 0) -> None:
        if depth > 7:
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized = _fold(key).replace(" ", "_")
                if normalized in {"listing_id", "id_publicacion", "publication_id", "codigo_publicacion"} and child not in (None, ""):
                    ids.add(str(child).strip())
                else:
                    visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
    visit(master.get("publicaciones"))
    return ids


def _comparables(master: Mapping[str, Any], operation: str) -> tuple[dict[str, Any], int, int]:
    analysis = master.get("analisis_comparables")
    if not isinstance(analysis, Mapping) or str(analysis.get("version") or "") != "cluster_v2_20260921":
        raise LiveTestCaseBuildError("current_v2_evidence_unavailable")
    segment = analysis.get("segmento") if isinstance(analysis.get("segmento"), Mapping) else {}
    if str(segment.get("operacion") or "").strip().upper() != operation:
        raise LiveTestCaseBuildError("comparable_operation_mismatch")
    market = analysis.get("mercado") if isinstance(analysis.get("mercado"), Mapping) else {}
    client = analysis.get("client_evidence") if isinstance(analysis.get("client_evidence"), Mapping) else {}
    level = str(client.get("evidence_level") or "INSUFFICIENT").strip().upper()
    if level not in {"HIGH", "MEDIUM", "LIMITED", "INSUFFICIENT"}:
        raise LiveTestCaseBuildError("comparable_quality_invalid")
    effective = "PARCEL_WITH_IMPROVEMENTS" if "parcela" in _fold(_property_type(master)) and _dimensions(master)["built"] else "PARCEL_LAND_ONLY" if any(x in _fold(_property_type(master)) for x in ("parcela", "sitio")) else "HOUSE"
    primary = str(market.get("price_surface") or "").strip().casefold()
    if effective == "PARCEL_LAND_ONLY":
        primary_surface = "land_m2"
    elif effective == "PARCEL_WITH_IMPROVEMENTS":
        # For improved parcels, built references are integral; bare-land
        # references remain explicitly secondary in ``land_references``.
        primary_surface = "built_m2"
    else:
        primary_surface = "land_m2" if primary in {"land_m2", "superficie_terreno", "uf_m2_land"} else "surface_ref_m2" if operation == ARRIENDO or primary in {"surface_ref_m2", "surface_util_m2", "superficie_util_m2"} else "built_m2"
    own_ids = _own_listing_ids(master)
    raw = analysis.get("comparables") if isinstance(analysis.get("comparables"), list) else []
    dedup: dict[str, Mapping[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        listing_id = str(item.get("listing_id") or item.get("source_id") or "").strip()
        if not listing_id or listing_id in own_ids:
            continue
        comp_operation = str(item.get("operation") or item.get("operacion") or operation).strip().upper()
        if comp_operation != operation:
            continue
        if listing_id not in dedup:
            dedup[listing_id] = item
    ordered = sorted(dedup.values(), key=lambda item: (
        _number(item.get("distance")) is None,
        _number(item.get("distance")) if _number(item.get("distance")) is not None else float("inf"),
    ))
    if len(ordered) > 20:
        ordered = ordered[:20]
    integral: list[dict[str, Any]] = []
    land: list[dict[str, Any]] = []
    for item in ordered:
        built = _number(item.get("superficie_construida") or item.get("built_m2"))
        land_m2 = _number(item.get("superficie_terreno") or item.get("land_m2"))
        comp = {
            "listing_id": str(item.get("listing_id") or item.get("source_id") or ""),
            "portal": item.get("portal"),
            "price_uf": _number(item.get("precio_uf") or item.get("price_uf")),
            "built_m2": built,
            "land_m2": land_m2,
            "bedrooms": _number(item.get("dormitorios") or item.get("bedrooms")),
            "bathrooms": _number(item.get("banos") or item.get("bathrooms")),
            "price_m2": _number(item.get("precio_m2") or item.get("price_m2")),
            "price_m2_built": _number(item.get("uf_m2_built") or item.get("precio_m2_construido") or item.get("price_m2_built")),
            "price_m2_land": _number(item.get("uf_m2_land") or item.get("precio_m2_terreno") or item.get("price_m2_land")),
            "distance": _number(item.get("distance")),
        }
        if effective == "PARCEL_LAND_ONLY":
            land.append(comp)
        elif built and built > 0 and comp["price_uf"] and (comp["price_m2_built"] or comp["price_m2"]):
            comp["status"] = "FULL_MATCH"
            integral.append(comp)
        elif effective == "PARCEL_WITH_IMPROVEMENTS" and land_m2 and land_m2 > 0:
            comp["status"] = "LAND_REFERENCE_ONLY"
            land.append(comp)
    if effective == "PARCEL_LAND_ONLY" and not integral:
        integral, land = land, []
        primary_surface = "land_m2"
    own_excluded = bool(own_ids) and not any(str(item.get("listing_id") or "") in own_ids for item in ordered)
    if not own_excluded:
        raise LiveTestCaseBuildError("own_listing_exclusion_unverified")
    v3 = {
        "effective_type": effective,
        "primary_surface": primary_surface,
        "secondary_surface": "land_m2" if land else None,
        "evidence_level": level,
        "full_match_count": sum(item.get("status") == "FULL_MATCH" for item in integral),
        "partial_match_count": sum(item.get("status") == "PARTIAL_MATCH" for item in integral),
        "land_reference_only_count": len(land),
        "comparable_display_status": "FULL" if level in {"HIGH", "MEDIUM"} else "LIMITED" if level == "LIMITED" else "HIDDEN",
        "evidence_conflict": bool(client.get("evidence_conflict")),
        "integral_comparables": integral,
        "land_references": land,
    }
    return v3, len(integral), len(land)


def _appraisal(db: Any, master: Mapping[str, Any], operation: str, current_price: float) -> dict[str, Any] | None:
    code = str(master.get("codigo") or "").strip()
    doc = db[APPRAISAL_COLLECTION].find_one({"codigo_propiedad": {"$in": _variants(code)}}, {
        "_id": 0, "codigo_propiedad": 1, "fecha_tasacion": 1, "updated_at": 1,
        "tasacion_online": 1, "analisis_comercial": 1,
    })
    online = (doc or {}).get("tasacion_online") or {}
    if operation == VENTA:
        commercial = online.get("valor_comercial") or {}
        limits = online.get("valor_minimo_maximo") or {}
        mid = _number(commercial.get("uf"))
        low = _number(limits.get("precio_minimo_uf"))
        high = _number(limits.get("precio_maximo_uf"))
        if mid is not None:
            low = low if low is not None else mid
            high = high if high is not None else mid
        elif low is not None and high is not None:
            mid = (low + high) / 2
    else:
        rent = online.get("arriendo_estimado") or {}
        mid = next((_number(rent.get(key)) for key in ("uf", "valor_uf", "estimado_uf") if _number(rent.get(key)) is not None), None)
        low = high = mid
    if not doc or all(value is None for value in (low, mid, high)):
        return None
    position = "ABOVE_RANGE" if high is not None and current_price > high else "BELOW_RANGE" if low is not None and current_price < low else "WITHIN_RANGE" if low is not None and high is not None else "NEAR_REFERENCE"
    return {
        "estimated_low_uf": low, "estimated_mid_uf": mid, "estimated_high_uf": high,
        "current_price_uf": current_price, "position_vs_appraisal": position,
        "document_date": doc.get("fecha_tasacion") or doc.get("updated_at"),
    }


def _communal(db: Any, master: Mapping[str, Any], operation: str) -> dict[str, Any] | None:
    commune, prop_type = _commune(master), _property_type(master)
    matches = []
    for row in db[COMMUNAL_COLLECTION].find({}, {
        "_id": 0, "comuna": 1, "tipo_propiedad": 1, "mercado_venta": 1,
        "mercado_arriendo": 1, "indicadores_mercado": 1, "updated_at": 1,
        "fecha_actualizacion": 1, "as_of": 1,
    }):
        if _fold(row.get("comuna")) == _fold(commune) and _fold(row.get("tipo_propiedad")) == _fold(prop_type):
            matches.append(row)
    if len(matches) != 1:
        return None
    row = matches[0]
    section = row.get("mercado_venta" if operation == VENTA else "mercado_arriendo")
    if not isinstance(section, Mapping):
        return None
    n_obs = _number(section.get("n_observations") or section.get("publicaciones_activas") or section.get("publicaciones_arriendo_activas"))
    if n_obs is not None and n_obs < 5:
        return None
    metrics = dict(section)
    metrics.update({key: value for key, value in (row.get("indicadores_mercado") or {}).items() if key not in metrics})
    return {
        "commune": commune, "property_type": prop_type, "operation": operation,
        "relevant_metrics": metrics, "document_date": row.get("updated_at") or row.get("fecha_actualizacion") or row.get("as_of"),
    }


def _support(db: Any, master: Mapping[str, Any], operation: str, current: float) -> tuple[str, dict[str, Any]]:
    appraisal = _appraisal(db, master, operation, current)
    communal = _communal(db, master, operation)
    doc_type = "INDIVIDUAL_APPRAISAL" if appraisal else "COMMUNAL_MARKET_REPORT" if communal else "NONE"
    support = {"operation": operation, "appraisal": appraisal, "communal_market": communal}
    return doc_type, support


def _campaign_segment(master: Mapping[str, Any]) -> str | None:
    for value in (
        master.get("campaign_evidence_segment"), _path(master, "analisis_comparables.campaign_evidence_segment"),
        _path(master, "owner_campaign.evidence_segment"), _path(master, "analisis_comparables.client_evidence.campaign_segment"),
    ):
        segment = str(value or "").strip().upper()
        if segment in CAMPAIGN_SEGMENTS:
            return segment
    return None


def _date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _origin(lead: Mapping[str, Any]) -> str:
    raw = _fold(lead.get("origen") or lead.get("portal") or lead.get("source") or _path(lead, "prospecto.origen"))
    compact = raw.replace(" ", "")
    for hint, name in (("portalinmobiliario", "PortalInmobiliario"), ("mercadolibre", "MercadoLibre"), ("yapo", "Yapo"), ("toctoc", "TocToc"), ("chilepropiedades", "ChilePropiedades"), ("proppit", "Proppit"), ("whatsapp", "WhatsApp/Directo")):
        if hint in compact:
            return name
    return "Otro"


def _activity_90d(db: Any, code: str, now: datetime) -> dict[str, Any]:
    cutoff = now - timedelta(days=90)
    values = _variants(code)
    unknown = False
    counts: Counter[str] = Counter()
    conversations: set[str] = set()
    visits: set[str] = set()
    try:
        leads = list(db["leads"].find({"prospecto.codigo": {"$in": values}}, {"_id": 1, "prospecto": 1, "created_at": 1, "conversation_id": 1, "origen": 1, "portal": 1, "source": 1}))
        lead_by_id: dict[str, str] = {}
        native_ids = []
        total = 0
        for lead in leads:
            linked_code = str(_path(lead, "prospecto.codigo") or "").strip()
            if linked_code != code:
                continue
            lead_by_id[str(lead.get("_id"))] = linked_code
            native_ids.append(lead.get("_id"))
            created = _date(lead.get("created_at"))
            if created is None:
                unknown = True
            elif cutoff <= created <= now:
                counts[_origin(lead)] += 1
                total += 1
        event_or = [{"property_code": {"$in": values}}]
        if native_ids:
            event_or.append({"lead_id": {"$in": native_ids}})
        events = list(db["conversation_events"].find({"$or": event_or}, {
            "lead_id": 1, "property_code": 1, "conversation_id": 1, "timestamp": 1,
            "created_at": 1, "event_type": 1, "actor_type": 1, "test_mode": 1, "source": 1,
        }))
        for event in events:
            if event.get("test_mode") is True or event.get("source") == "owner_campaign_test":
                continue
            event_code = str(event.get("property_code") or lead_by_id.get(str(event.get("lead_id"))) or "").strip()
            if event_code != code:
                continue
            event_type = str(event.get("event_type") or "").casefold()
            actor = str(event.get("actor_type") or "").casefold()
            if event_type not in {"customer_message_received", "human_message_sent"} and actor not in {"customer", "owner"}:
                continue
            event_date = _date(event.get("timestamp") or event.get("created_at"))
            if event_date is None:
                unknown = True
            elif cutoff <= event_date <= now:
                conversation_id = str(event.get("conversation_id") or "").strip()
                if conversation_id:
                    conversations.add(conversation_id)
                else:
                    unknown = True
        orders = list(db["visitas"].find({"property_code": {"$in": values}}, {"property_code": 1, "status": 1, "timeline": 1, "visita_code": 1, "_id": 1}))
        for visit in orders:
            if str(visit.get("property_code") or "").strip() != code or str(visit.get("status") or "").casefold() != "signed":
                continue
            accepted = None
            for item in visit.get("timeline") or []:
                if isinstance(item, Mapping) and str(item.get("action") or "").casefold() == "accepted":
                    accepted = _date(item.get("server_timestamp"))
                    break
            if accepted is None:
                unknown = True
            elif cutoff <= accepted <= now:
                visits.add(str(visit.get("visita_code") or visit.get("_id") or ""))
        if unknown:
            return {"state": "UNKNOWN", "total_leads": None, "portals": [], "conversations": None, "visits": None}
        state = "KNOWN_POSITIVE" if total else "KNOWN_ZERO"
        return {
            "state": state,
            "total_leads": total,
            "portals": [{"name": portal, "count": counts[portal]} for portal in PORTALS if counts[portal]],
            "conversations": len(conversations), "visits": len(visits),
        }
    except Exception:
        return {"state": "UNKNOWN", "total_leads": None, "portals": [], "conversations": None, "visits": None}


def _segment_copy(segment: str, operation: str, authorization: bool) -> dict[str, str]:
    rental = operation == ARRIENDO
    copies = {
        "STRONG_PRICE_ADJUSTMENT": {
            "headline": "Referencias convergentes para revisar un nuevo valor",
            "body": "La tasación y las publicaciones comparables disponibles entregan señales compatibles para evaluar el valor sugerido. Son referencias comerciales; no garantizan un precio de cierre.",
            "cta": "ACEPTAR NUEVO VALOR", "cta_type": "PRICE_AUTHORIZATION",
        },
        "MIXED_EVIDENCE": {
            "headline": "Las referencias muestran señales mixtas",
            "body": "La información disponible no apunta en una sola dirección. Revisemos las referencias de esta propiedad antes de definir cualquier cambio.",
            "cta": "REVISAR RECOMENDACIÓN CON MI ASESOR", "cta_type": "ADVISOR_REVIEW",
        },
        "COMPETITIVE_LOW_RESPONSE": {
            "headline": "Precio competitivo con baja respuesta reciente",
            "body": "El precio aparece competitivo frente a las referencias disponibles, aunque la actividad comercial verificable de los últimos 90 días fue baja. Podemos revisar juntos la estrategia de difusión.",
            "cta": "REVISAR AJUSTE CON MI ASESOR", "cta_type": "ADVISOR_REVIEW",
        },
        "INSUFFICIENT_EVIDENCE": {
            "headline": "Información comercial limitada",
            "body": "Con los antecedentes disponibles no es posible concluir que el precio esté sobre el mercado. Tu asesor puede revisar el posicionamiento y los próximos pasos contigo.",
            "cta": "REVISAR POSICIONAMIENTO CON MI ASESOR", "cta_type": "ADVISOR_REVIEW",
        },
    }
    if segment == "TEST_ADVISOR_REVIEW":
        return {
            "headline": "Revisión con tu asesor",
            "body": "Las referencias de esta propiedad se presentan para una revisión individual con tu asesor. No se está proponiendo un cambio automático de precio.",
            "cta": "REVISAR RECOMENDACIÓN CON MI ASESOR", "cta_type": "ADVISOR_REVIEW",
        }
    content = dict(copies.get(segment, copies["INSUFFICIENT_EVIDENCE"]))
    if segment == "STRONG_PRICE_ADJUSTMENT" and not authorization:
        content.update({
            "headline": "Referencias favorables para una revisión comercial",
            "body": "La evidencia disponible permite revisar el posicionamiento con tu asesor, pero no habilita por sí sola una autorización automática de nuevo precio.",
            "cta": "REVISAR RECOMENDACIÓN CON MI ASESOR", "cta_type": "ADVISOR_REVIEW",
        })
    if rental:
        content["body"] = content["body"].replace("tasación", "estimación de arriendo").replace("precio de cierre", "valor final de contrato")
    return content


def _build_case(db: Any, master: Mapping[str, Any], case_id: str, *, segment: str, raw_target: float | None = None, now: datetime) -> OwnerCampaignTestCase:
    code = str(master.get("codigo") or "").strip()
    if not code or not _active_available(master) or not _is_sucre(master):
        raise LiveTestCaseBuildError("property_not_current_active_available_sucre")
    # A/B/D are sent only to the fixed test inbox; owner email is useful for
    # attribution when present but is not needed to render or route test mail.
    # Portfolio grouping (E) still requires a real owner email for identity.
    email = _email_from_property(master, required=case_id == "E")
    executive = _resolve_executive(db, master)
    operation = resolve_property_operation(master)
    if case_id == "D" and operation == VENTA_ARRIENDO:
        operation = ARRIENDO
    if operation not in {VENTA, ARRIENDO}:
        raise LiveTestCaseBuildError("property_operation_unresolved")
    price_block = operation_price_block(master, requested_operation=operation)
    current = _number((price_block or {}).get("precio_uf"))
    if current is None or current <= 0:
        raise LiveTestCaseBuildError("live_price_unavailable")
    prop_type, commune = _property_type(master), _commune(master)
    dimensions = _dimensions(master)
    v3, integral_n, land_n = _comparables(master, operation)
    analysis = master["analisis_comparables"]
    market = analysis.get("mercado") if isinstance(analysis.get("mercado"), Mapping) else {}
    quality = str((analysis.get("client_evidence") or {}).get("evidence_level") or "INSUFFICIENT").upper()
    percentile = _number(market.get("property_percentile"))
    own_ids = _own_listing_ids(master)
    doc_type, support = _support(db, master, operation, current)

    if case_id == "A":
        adjustment = (current - raw_target) / current * 100 if raw_target else -1
        appraisal = support.get("appraisal") or {}
        appraisal_high = _number(appraisal.get("estimated_high_uf"))
        if (
            operation != VENTA or doc_type != "INDIVIDUAL_APPRAISAL"
            or quality not in {"HIGH", "MEDIUM"} or integral_n < 8
            or percentile is None or percentile < 75
            or str(market.get("price_surface") or "").strip().casefold() not in {"built_m2", "superficie_construida", "uf_m2_built"}
            or appraisal_high is None or not math.isclose(appraisal_high, float(raw_target), abs_tol=0.05)
            or not 2 <= adjustment <= 10
        ):
            raise LiveTestCaseBuildError("case_a_live_evidence_not_authorized")
        expected_segment, cta_type = "STRONG_PRICE_ADJUSTMENT", "PRICE_AUTHORIZATION"
    elif case_id == "B":
        appraisal = support.get("appraisal") or {}
        low, mid, high = (_number(appraisal.get(key)) for key in ("estimated_low_uf", "estimated_mid_uf", "estimated_high_uf"))
        if operation != VENTA or v3.get("primary_surface") != "built_m2" or integral_n < 8 or percentile is None or percentile >= 75 or dimensions["built"] is None or abs(dimensions["built"] - 64) > .1 or mid is None or high is None or not mid < current <= high:
            raise LiveTestCaseBuildError("case_b_live_mixed_evidence_contract_failed")
        if low is not None and abs(low - 1009) > 25 or mid is not None and abs(mid - 1442) > 25 or high is not None and abs(high - 1809) > 25:
            raise LiveTestCaseBuildError("case_b_appraisal_changed_from_approved_case")
        expected_segment, cta_type = "MIXED_EVIDENCE", "ADVISOR_REVIEW"
    elif case_id == "C":
        if (
            operation != VENTA or "parcela" not in _fold(prop_type)
            or v3.get("primary_surface") != "built_m2"
            or dimensions["built"] is None or dimensions["land"] is None
            or abs(dimensions["built"] - 560) > 1 or abs(dimensions["land"] - 5000) > 1
            or integral_n < 3 or land_n < 1
        ):
            raise LiveTestCaseBuildError("case_c_parcel_improvements_contract_failed")
        expected_segment, cta_type = "INSUFFICIENT_EVIDENCE", "ADVISOR_REVIEW"
    elif case_id == "D":
        estimate = (support.get("appraisal") or {}).get("estimated_mid_uf")
        if operation != ARRIENDO or not _number(estimate) or not v3.get("integral_comparables"):
            raise LiveTestCaseBuildError("case_d_rental_sources_unavailable")
        expected_segment = _campaign_segment(master) or "TEST_ADVISOR_REVIEW"
        cta_type = "ADVISOR_REVIEW"
    else:
        stored = _campaign_segment(master)
        expected_segment = stored or "TEST_ADVISOR_REVIEW"
        cta_type = "ADVISOR_REVIEW"

    existing_segment = _campaign_segment(master)
    if case_id in {"A", "B", "C"} and existing_segment and existing_segment != expected_segment:
        raise LiveTestCaseBuildError("fixed_case_segment_conflicts_with_live_source")
    if case_id == "D" and expected_segment not in CAMPAIGN_SEGMENTS | {"TEST_ADVISOR_REVIEW"}:
        raise LiveTestCaseBuildError("case_d_segment_invalid")

    adjustment_pct = -((current - float(raw_target)) / current * 100) if raw_target else None
    prop = {
        "codigo_propiedad": code, "codigo": code, "tipo_propiedad": prop_type,
        "comuna": commune, "operacion": operation, "precio_publicado_uf": current,
        "superficie_construida": dimensions["built"], "superficie_util": dimensions["built"],
        "superficie_terreno": dimensions["land"], "dormitorios": dimensions["bedrooms"],
        "banos": dimensions["bathrooms"], "estacionamientos": dimensions["parking"],
    }
    support_view = {
        "supporting_evidence": support,
        "client_validation_v3": v3,
    }
    content = _segment_copy(expected_segment, operation, cta_type == "PRICE_AUTHORIZATION")
    qa_row = {
        "codigo": code, "tipo": prop_type, "comuna": commune, "operacion": operation,
        "comparables_quality": quality, "evidence_segment": expected_segment,
        "recommendation": "con ajuste de precio sustentado" if cta_type == "PRICE_AUTHORIZATION" else "revisión con asesor",
        "diagnostic": "ALIGNED" if cta_type == "PRICE_AUTHORIZATION" else "REVIEW",
        "document_type": doc_type, "attachment": {"status": "ok"} if doc_type != "NONE" else {"status": "missing"},
        "segment_copy": content, "cta": {"primary_label": content["cta"], "primary_url": "", "secondary_url": ""},
    }
    if cta_type == "PRICE_AUTHORIZATION":
        qa_row["nuevo_precio_objetivo_uf"] = raw_target
    from analytics.owner_campaign_email_v2 import _initials, _property_context

    executive["initials"] = _initials(executive["name"])
    image = _resolve_image(master, code)
    model = _property_context(prop, qa_row, support_view, executive, image)
    model["executive"] = executive
    model["activity_90d"] = _activity_90d(db, code, now)
    model["campaign_segment"] = expected_segment
    if operation == ARRIENDO and doc_type == "INDIVIDUAL_APPRAISAL":
        model["document"] = dict(model.get("document") or {})
        model["document"]["copy"] = "Estimación de arriendo individual disponible como respaldo comercial."
    if cta_type != "PRICE_AUTHORIZATION":
        model["recommended_price_label"] = None
        model["display_adjustment_label"] = None
        model["appraisal"] = dict(model.get("appraisal") or {})
        model["appraisal"].pop("recommended_label", None)
        model["appraisal"].pop("adjustment_label", None)
    else:
        model["recommended_price_label"] = f"{raw_target:,.0f} UF".replace(",", ".")
        model["display_adjustment_label"] = f"{adjustment_pct:+.1f}%".replace(".", ",")
        model["appraisal"] = dict(model.get("appraisal") or {})
        model["appraisal"]["recommended_label"] = model["recommended_price_label"]
        model["appraisal"]["adjustment_label"] = model["display_adjustment_label"]
    price_percentile_label = _number(market.get("property_percentile"))
    source_checks = {
        "real_property_image": image.get("available") is True,
        "executive_name": bool(executive.get("name")), "executive_email": bool(executive.get("email")),
        "executive_phone": bool(executive.get("phone")), "activity_90d": True,
        "portal_breakdown_valid": True, "reference_correct": True,
        "comparables_compatible": str((analysis.get("segmento") or {}).get("operacion") or "").upper() == operation,
        "own_listing_excluded": bool(own_ids) and not any(str(item.get("listing_id") or "") in own_ids for item in v3["integral_comparables"] + v3["land_references"]),
        "cta_correct": content["cta_type"] == cta_type,
        "price_percentile": price_percentile_label,
        "primary_surface": v3["primary_surface"],
        "integral_comparables_n": integral_n,
        "land_references_n": land_n,
    }
    # The established structural classification remains STRONG_PRICE_ADJUSTMENT;
    # PRICE_AUTHORIZATION_READY is the test-ledger/action contract for A only.
    test_segment = "PRICE_AUTHORIZATION_READY" if case_id == "A" else expected_segment
    return OwnerCampaignTestCase(
        case_id=case_id, property_code=code, intended_owner_email=email,
        operation=operation, evidence_segment=test_segment, document_type=doc_type,
        executive=executive["name"], current_price=current, cta_type=cta_type,
        raw_recommended_price=raw_target, display_recommended_price=raw_target,
        adjustment_pct=adjustment_pct, commune=commune, property_type=prop_type,
        render_context={"property_model": model, "executives": [executive], "source_checks": source_checks},
    )


def _load_code_docs(db: Any, codes: set[str]) -> dict[str, dict[str, Any]]:
    found = list(db[PROPERTY_COLLECTION].find({"codigo": {"$in": [value for code in codes for value in _variants(code)]}}))
    indexed = {str(doc.get("codigo") or "").strip(): doc for doc in found}
    if set(indexed) != codes:
        raise LiveTestCaseBuildError("required_property_not_found")
    return indexed


def _build_portfolio(db: Any, now: datetime) -> OwnerCampaignTestCase:
    fixed_case_codes = set(PROPERTY_CODES.values())
    candidates = []
    for master in db[PROPERTY_COLLECTION].find({"estado.estado_prop360": "Activa", "disponible_prop360": True}):
        code = str(master.get("codigo") or "").strip()
        if code in fixed_case_codes or code in EXPLICITLY_EXCLUDED_CODES:
            continue
        if not _active_available(master) or not _is_sucre(master):
            continue
        try:
            email = _email_from_property(master)
            executive_name = str(_path(master, "estado.ejecutivo") or "").strip()
            if not executive_name:
                continue
            candidates.append((email, _fold(executive_name), master))
        except LiveTestCaseBuildError:
            continue
    by_owner: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for email, executive, master in candidates:
        by_owner[(email, executive)].append(master)
    groups = sorted((items for items in by_owner.values() if len(items) >= 3), key=lambda items: (-len(items), str(items[0].get("codigo") or "")))
    for group in groups[:20]:
        cases: list[OwnerCampaignTestCase] = []
        for master in sorted(group, key=lambda item: str(item.get("codigo") or "")):
            try:
                segment = _campaign_segment(master)
                # A portfolio preview must retain each property's live segment.
                # Without it, silently assigning a test-only segment would no
                # longer represent the owner's actual campaign evidence.
                if segment not in CAMPAIGN_SEGMENTS:
                    continue
                case = _build_case(db, master, "E", segment=segment, now=now)
                if case.document_type not in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}:
                    continue
                case = OwnerCampaignTestCase(**{**case.__dict__, "cta_type": "ADVISOR_REVIEW", "raw_recommended_price": None, "display_recommended_price": None, "adjustment_pct": None})
                model = dict(case.render_context["property_model"])
                # E never invents a numeric target. Even if this property is
                # structurally strong, the compact test card asks for advisor
                # review unless that property's own approved target is present.
                content = _segment_copy(segment, case.operation, False)
                model.update({
                    "campaign_segment": segment,
                    "recommendation": "revisión con asesor",
                    "source_recommendation": "revisión con asesor",
                    "recommendation_text": content["body"],
                    "recommendation_title": content["headline"],
                    "single_diagnostic_text": content["body"],
                    "diagnostic_text": content["body"],
                    "recommended_price_label": None,
                    "display_adjustment_label": None,
                    "cta": {**dict(model.get("cta") or {}), "primary_url": "", "primary_label": content["cta"]},
                })
                case = OwnerCampaignTestCase(**{**case.__dict__, "render_context": {**case.render_context, "property_model": model, "source_checks": {**case.render_context["source_checks"], "cta_correct": True}}})
                cases.append(case)
            except LiveTestCaseBuildError:
                continue
        if len(cases) >= 3 and len({item.intended_owner_email for item in cases}) == 1 and len({item.executive for item in cases}) == 1:
            selected = tuple(cases[: min(9, len(cases))])
            first = selected[0]
            return OwnerCampaignTestCase(**{**first.__dict__, "case_id": "E", "portfolio_cases": selected, "render_context": {"executives": [first.render_context["property_model"]["executive"]]}})
    raise LiveTestCaseBuildError("current_multi_property_owner_group_unavailable")


def build_owner_campaign_test_cases_live(
    db: Any = None,
    *,
    now: datetime | None = None,
    case_ids: Sequence[str] | None = None,
) -> list[OwnerCampaignTestCase]:
    """Build a fixed subset of live E2E cases, defaulting to the complete A-E set."""
    if db is None:
        from chatbot.storage import get_db

        db = get_db()
    requested = tuple(("A", "B", "C", "D", "E") if case_ids is None else case_ids)
    if not requested or len(requested) != len(set(requested)) or set(requested) - {"A", "B", "C", "D", "E"}:
        raise LiveTestCaseBuildError("requested_test_cases_invalid")
    current_time = now or datetime.now(timezone.utc)
    fixed_ids = [case_id for case_id in requested if case_id != "E"]
    docs = _load_code_docs(db, {PROPERTY_CODES[case_id] for case_id in fixed_ids}) if fixed_ids else {}
    builders = {
        "A": lambda: _build_case(db, docs[PROPERTY_CODES["A"]], "A", segment="STRONG_PRICE_ADJUSTMENT", raw_target=APPROVED_A_RAW_TARGET_UF, now=current_time),
        "B": lambda: _build_case(db, docs[PROPERTY_CODES["B"]], "B", segment="MIXED_EVIDENCE", now=current_time),
        "C": lambda: _build_case(db, docs[PROPERTY_CODES["C"]], "C", segment="INSUFFICIENT_EVIDENCE", now=current_time),
        "D": lambda: _build_case(db, docs[PROPERTY_CODES["D"]], "D", segment="TEST_ADVISOR_REVIEW", now=current_time),
        "E": lambda: _build_portfolio(db, current_time),
    }
    return [builders[case_id]() for case_id in requested]
