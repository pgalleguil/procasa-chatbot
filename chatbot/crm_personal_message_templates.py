"""Private reusable WhatsApp templates, scoped by authenticated CRM user ID."""
from datetime import datetime, timezone
import re
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

COLLECTION = "crm_personal_message_templates_v1"
MAX_TEMPLATES = 30
PABLO_COMPOSER_PILOT_USER_ID = "69796bc4bbebf240378eb739"
PILOT_BUILTIN_KEYS = frozenset(f"builtin:{i}" for i in range(1, 14))


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
    if set(re.findall(r"\[\[([^\[\]]+)\]\]", body)) - (VARIABLES | {"operacion"} if pilot else VARIABLES):
        raise ValueError("Variable no admitida")
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
        result["favorites"] = changes.get("favorites", read_library(db, user_id)["favorites"])
    return result
