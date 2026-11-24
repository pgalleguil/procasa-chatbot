"""Private reusable WhatsApp templates, scoped by authenticated CRM user ID."""
from datetime import datetime, timezone
import re
import math
import unicodedata
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

COLLECTION = "crm_personal_message_templates_v1"
MAX_TEMPLATES = 30
PABLO_COMPOSER_PILOT_USER_ID = "69796bc4bbebf240378eb739"
PILOT_BUILTIN_KEYS = frozenset(f"builtin:{i}" for i in range(1, 30))


def composer_pilot_enabled(user_id):
    return str(user_id or "") == PABLO_COMPOSER_PILOT_USER_ID

VARIABLES = frozenset(("nombre", "ejecutivo", "propiedad", "comuna", "enlace"))
CATEGORIES = frozenset(("stage1", "stage2", "stage3", "stage4", "stage5", "stage6"))


def validate_template(data, *, pilot=False):
    if not isinstance(data, dict):
        raise ValueError("Plantilla inválida")
    title = data.get("title")
    body = data.get("body")
    category = data.get("category", "stage1")
    if not isinstance(title, str) or not isinstance(body, str) or not isinstance(category, str):
        raise ValueError("Plantilla inválida")
    title, body = title.strip(), body.strip()
    if not 1 <= len(title) <= 70 or not 1 <= len(body) <= 2000 or category not in CATEGORIES:
        raise ValueError("Nombre, contenido o categoría inválidos")
    if re.search(r"https?://|www\.|(?:wa\.me/)|(?:\+?56[ -]?9(?:[ -]?\d){8})", title + "\n" + body, re.I):
        raise ValueError("Usa [[enlace]] y evita teléfonos o enlaces fijos de clientes")
    if set(re.findall(r"\[\[([^\[\]]+)\]\]", body)) - (VARIABLES | {"operacion", "consulta", "saludo", "origen", "referencia", "atributos", "visita_paso", "postulacion_paso", "costos", "financiamiento_paso", "orden_visita", "oferta_paso", "seguimiento_info", "busqueda_paso"} if pilot else VARIABLES):
        raise ValueError("Variable no admitida")
    if pilot and category == "stage1" and "[[enlace]]" not in body:
        body = body.rstrip() + "\n\n[[enlace]]"
        if len(body) > 2000:
            raise ValueError("El mensaje de primer contacto debe incluir el enlace automático")
    return {"title": title, "body": body, "category": category}


def read_library(db, user_id):
    doc = db[COLLECTION].find_one({"_id": str(user_id)}) or {}
    result = {"version": doc.get("version", 0), "templates": doc.get("templates", [])}
    if composer_pilot_enabled(user_id):
        allowed = PILOT_BUILTIN_KEYS | {f"personal:{item['id']}" for item in result["templates"]}
        result["favorites"] = [key for key in doc.get("favorites", []) if key in allowed]
    return result


def write_library(db, user_id, payload):
    """Compare-and-swap prevents one browser session overwriting another."""
    if not isinstance(payload, dict):
        raise ValueError("Solicitud inválida")
    version = payload.get("version")
    items = payload.get("templates")
    if type(version) is not int or version < 0 or not isinstance(items, list) or len(items) > MAX_TEMPLATES:
        raise ValueError("Máximo 30 plantillas por ejecutivo")
    pilot = composer_pilot_enabled(user_id)
    if "favorites" in payload and not pilot:
        raise ValueError("Favoritos no habilitados para esta cuenta")
    templates = []
    seen = set()
    for item in items:
        validated = validate_template(item, pilot=pilot)
        ident = item.get("id") or uuid4().hex
        if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident) or ident in seen:
            raise ValueError("Identificador de plantilla inválido")
        seen.add(ident)
        templates.append({"id": ident, **validated})
    changes = {"templates": templates, "updated_at": datetime.now(timezone.utc)}
    if pilot and "favorites" in payload:
        favorites = payload["favorites"]
        allowed = PILOT_BUILTIN_KEYS | {f"personal:{ident}" for ident in seen}
        if not isinstance(favorites, list) or len(favorites) > len(allowed) or any(
            not isinstance(key, str) or key not in allowed for key in favorites
        ) or len(set(favorites)) != len(favorites):
            raise ValueError("Favoritos inválidos")
        changes["favorites"] = favorites
    try:
        result = db[COLLECTION].update_one(
            {"_id": str(user_id), "version": version},
            {"$set": changes,
             "$inc": {"version": 1}},
            upsert=(version == 0),
        )
    except DuplicateKeyError:
        return None
    if not (result.matched_count or result.upserted_id is not None):
        return None
    result = {"version": version + 1, "templates": templates}
    if pilot:
        result["favorites"] = changes["favorites"] if "favorites" in changes else read_library(db, user_id)["favorites"]
    return result


def _pilot_text(value):
    return "".join(c for c in unicodedata.normalize("NFD", str(value or "").casefold())
                   if unicodedata.category(c) != "Mn").strip()


def _pilot_datetime(value):
    from chatbot.crm_metrics import coerce_utc_datetime
    try:
        return coerce_utc_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _pilot_amount(value):
    if type(value) in (int, float) and math.isfinite(value) and value > 0:
        return value
    return None


def build_pilot_message_context(lead, detail, db, *, now=None):
    """Bounded read-only context for the authenticated pilot. Never use bot claims
    as evidence of a customer's finances or confirmed transaction outcomes.
    """
    now = now or datetime.now(timezone.utc)
    prop = detail.get("datos_propiedad") or {}
    prospect = lead.get("prospecto") or {}
    active = detail.get("active_search") or {}
    criteria = active.get("criteria") or {}
    evidence = active.get("field_evidence") or {}
    if active.get("status") != "ACTIVE" or active.get("opt_out"):
        criteria, evidence = {}, {}
    known = {}
    for field in ("comunas", "property_types", "max_price_clp", "max_price_uf",
                  "min_bedrooms", "bedrooms", "operation"):
        source = evidence.get(field) or {}
        if source.get("source") not in ("office_whatsapp", "executive_whatsapp"):
            continue
        value = criteria.get(field)
        if field in ("comunas", "property_types"):
            if isinstance(value, list):
                known[field] = [x.strip()[:60] for x in value[:8] if isinstance(x, str) and x.strip()]
        elif field == "operation":
            if str(value).upper() in ("VENTA", "ARRIENDO"):
                known[field] = str(value).upper()
        elif _pilot_amount(value):
            known[field] = value

    # Only explicit first-person statements in recent customer messages.
    # Questions, hedged statements and assistant/executive messages are not evidence.
    messages = (lead.get("messages") or [])[-60:]
    customer = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _pilot_text(msg.get("content"))[:500]
        if text and "?" not in text and not re.search(r"\b(si|quizas|tal vez|podria|creo|supongo)\b", text):
            customer.append(text)
    finance, employment, applicant = "", "", ""
    requested_visit = ""
    # Only a specific weekday and clock time in a recent positive customer
    # statement. Never turn this preference into a confirmed booking.
    if customer:
        latest = customer[-1]
        if not re.search(r"\b(no|cancel|reagendar|otro)\b", latest) and re.search(r"\b(puedo|me acomoda|me sirve|prefiero|podemos)\b", latest):
            match = re.search(r"\b(lunes|martes|miercoles|jueves|viernes|sabado|domingo)\s+(?:a\s+las\s+|a\s+la\s+)?([01]?\d|2[0-3]):([0-5]\d)\b", latest)
            if match:
                day = {"miercoles": "miércoles", "sabado": "sábado"}.get(match[1], match[1])
                requested_visit = f"{day} a las {int(match[2]):02d}:{match[3]} horas"

    for text in reversed(customer):
        if not finance:
            if re.search(r"\b(no tengo|aun no tengo|todavia no tengo) (un )?(credito )?(hipotecario )?preaprobado\b", text):
                finance = "not_preapproved"
            elif re.search(r"\b(tengo|cuento con|ya tengo) (un |el )?credito (hipotecario )?preaprobado\b", text) and not re.search(r"\b(no|sin|vencio|vencido|rechazado)\b", text):
                finance = "preapproved"
            elif re.search(r"\b(compro|comprare|pagare|compraria) al contado\b", text) and "no " not in text:
                finance = "cash"
            elif re.search(r"\b(estoy evaluando|estoy tramitando|estoy solicitando) (un |el )?credito\b", text):
                finance = "evaluating"
        if not employment:
            if re.search(r"\b(soy|trabajo como|trabajo de forma) (trabajador |trabajadora )?independiente\b", text) and "no " not in text:
                employment = "independent"
            elif re.search(r"\b(soy|trabajo como|trabajo de forma) (trabajador |trabajadora )?dependiente\b", text) and "no " not in text:
                employment = "dependent"
        if not applicant:
            if re.search(r"\b(postulo|postulare|postularia|arrendaria|arrendare) (solo|sola|individualmente)\b", text) and "no " not in text:
                applicant = "individual"
            elif re.search(r"\b(complemento|complementare|complementaria) renta\b", text) and "no " not in text:
                applicant = "joint"

    # Follow-up topic is derived only from the latest human outbound message,
    # never from the bot's promises or inferred pipeline state.
    followup = ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        if role.startswith("executive:") or role == "agent":
            txt = _pilot_text(msg.get("content"))[:1500]
            if re.search(r"liquidaciones|cotizaciones|carpeta tributaria|requisitos de arriendo", txt):
                followup = "rental_documents"
            elif re.search(r"preaprob|credito hipotecario", txt):
                followup = "financing"
            elif re.search(r"www\.procasa\.cl|https?://", txt):
                followup = "property_information"
            break

    visit = None
    visit_at = _pilot_datetime(lead.get("visit_date"))
    if (str(lead.get("pipeline_stage") or "").upper() == "VISIT_SCHEDULED"
            and detail.get("assignment_cycle_id") and detail.get("contact_visit_confirmed_in_cycle")
            and visit_at and visit_at > now):
        visit = visit_at.isoformat()

    operation = str(prop.get("contact_operation") or "").casefold()
    price = (prop.get("contact_public_facts") or {}).get(
        "precio_venta" if operation == "venta" else "precio_arriendo" if operation == "arriendo" else ""
    ) or {}
    currency = str(price.get("moneda_publicada") or price.get("moneda_publicacion") or "").upper()
    current_price = _pilot_amount(price.get("precio_publicado_original")) or _pilot_amount(price.get("precio_publicado"))
    drop = None
    code = str(prop.get("codigo") or "")
    created = _pilot_datetime(lead.get("created_at"))
    # Only a confirmed same-currency historical change for this exact property.
    # Dual operation or missing lead date: no automatic price-drop assertion.
    if code.isdigit() and prop.get("public_contact_url") and operation in ("venta", "arriendo") and current_price and currency in ("UF", "CLP") and created:
        try:
            event = db["universo_cartera_prop360_historial"].find_one(
                {"codigo": code, "tipo_evento": "cambio_precio", "campo": "precio_publicado"},
                sort=[("fecha", -1)],
            ) or {}
            before, after = _pilot_amount(event.get("monto_anterior")), _pilot_amount(event.get("monto_nuevo"))
            changed = _pilot_datetime(event.get("fecha"))
            verified = "bitacora" in _pilot_text(event.get("verification_method")) and "prop360" in _pilot_text(event.get("fuente"))
            if (verified and changed and created < changed <= now
                    and event.get("moneda_anterior") == currency == event.get("moneda_nueva")
                    and before and after and before > after == current_price):
                drop = {"before": before, "after": after, "currency": currency}
        except Exception:
            # Fail closed: unavailable price history must not prevent opening a lead.
            drop = None
    return {
        "criteria": known, "finance": finance, "employment": employment,
        "applicant": applicant, "followup": followup, "visit_at": visit,
        "price_drop": drop, "requested_visit": requested_visit,
        "name_known": bool(str(prospect.get("nombre") or "").strip() and _pilot_text(prospect.get("nombre")) not in ("desconocido", "none", "s/i", "cliente", "sin nombre")),
        "email_known": "@" in str(prospect.get("email") or ""),
        "rut_known": bool(re.fullmatch(r"\d{1,2}\.?\d{3}\.?\d{3}-?[\dkK]", str(prospect.get("rut") or "").strip())),
    }
