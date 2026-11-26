"""Conservative customer evidence; no I/O and no CRM state transitions.

Unsupported constructions abstain. Literal occurrence alone is never proof of
an affirmative request, a maximum budget, or permission to contact someone.
"""
from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from .geo_utils import COMMUNE_OFFICIAL_NAMES


def plain(value):
    return re.sub(r"\s+", " ", "".join(
        c for c in unicodedata.normalize("NFKD", str(value or "").lower())
        if not unicodedata.combining(c))).strip()


def ambiguous(text):
    return bool(re.search(
        r'\b(?:si acaso|si algun|si ya|supongamos|imagina|no dije|no he dicho|'
        r'nunca dije|no es cierto|mentira|sarcasmo|ironia|era broma|en broma|me equivoque|'
        r'me arrepenti|retiro lo|era un ejemplo|eso era antes|si me aprueban|si consigo|cuando tenga|si quisiera|si pudiera|'
        r'me dijo|dijo que|dice que|segun|ignora|instrucciones|clasifica|'
        r'responde json|system prompt)\b|\bno,? mejor\b|'
        r'\bmi (?:esposa|esposo|hijo|hija|amigo|amiga|hermano|hermana|vecino|vecina) '
        r'(?:quiere|busca|necesita|compro|arrendo)\b|["“”«»]', plain(text)))


def clauses(text):
    return [p.strip(' ¿?¡!.,:;') for p in re.split(
        r'\b(?:pero|sino|sin embargo|y despues)\b|\b(?:y|para|antes de) (?=comprar|vender|arrendar)'
        r'|[;!?\n]|(?<!\d)[.,](?!\d)', plain(text)) if p.strip()]


def intent(text, previous_agent=""):
    msg = plain(text)
    if ambiguous(msg):
        return "NO_SIGNAL"
    parts = clauses(text)
    # Only a complete imperative/request clause can assert a global stop.
    stop = (r'(?:por favor )?(?:'
            r'no me (?:escriban|escribas|contacten|contactes|manden mensajes)(?: nunca)?(?: mas)?|'
            r'no (?:quiero|deseo) recibir (?:mas )?(?:mensajes|avisos|promociones)|'
            r'no quiero mas (?:avisos|promociones|mensajes)|'
            r'no me (?:manden|envien) mas (?:avisos|promociones|mensajes)|'
            r'denme de baja|quiero que me den de baja|eliminen mi numero|stop'
            r')(?: por favor| gracias| nunca mas)*')
    if '?' not in msg and any(re.fullmatch(stop, p) for p in parts):
        return "OPT_OUT"
    active = any(re.match(r'(?:ahora |todavia |aun |sigo )?(?:busco|necesito|quiero comprar|quiero arrendar)\b', p) for p in parts)
    completed = r'(?:ya (?:encontre|compre|arrende|adquiri) (?:una |un |mi )?(?:propiedad|casa|departamento|inmueble)|ya no (?:estoy buscando|busco (?:casa|departamento|propiedad))|termine (?:mi |la )?busqueda)(?: gracias| por fin)*'
    if '?' not in msg and not active and any(re.fullmatch(completed, p) for p in parts):
        return "SEARCH_COMPLETED"
    # Explicitly deferred demand is not a completed purchase or unsubscribe.
    # Do not mistake a short scheduling conflict for an inactive search.
    deferred = re.search(
        r'\b(?:solo (?:estaba|estoy|ando) (?:mirando|cotizando)|'
        r'(?:por ahora|por el momento) (?:solo miro|no estoy buscando|no voy a comprar|no voy a arrendar)|'
        r'(?:no (?:puedo|podre|estoy en condiciones de|estare en condiciones de) (?:comprar|arrendar|ahorrar)|'
        r'(?:voy a|tengo que|necesito) ahorrar).{0,85}\b(?:proximo ano|ano que viene|unos anos|varios meses|mas adelante|un ano)|'
        r'(?:comprare|arrendare|buscare|retomare la busqueda) (?:el )?(?:proximo ano|ano que viene|mas adelante)|'
        r'mas adelante (?:quizas |tal vez )?(?:hablemos|les escribo|me contacto))\b', msg)
    if deferred and not active and '?' not in msg:
        return "SEARCH_DEFERRED"
    # A decision for another property is closure of the current search,
    # including common Chilean spelling mistakes. A comparison is not closure.
    chosen_other = re.search(r'\b(?:finalmente |ya )?(?:nos |me )?(?:decidimos|desidimos|decidi|desidi|quedamos|quede) (?:por|con) otr[oa] (?:departamento|depto|casa|propiedad|inmueble)\b', msg)
    if chosen_other and not active and '?' not in msg and not re.search(r'\b(?:no|todavia no|aun no) (?:nos |me )?(?:decidimos|desidimos|decidi|desidi|quedamos|quede)\b', msg):
        return "SEARCH_COMPLETED"
    # Polite standalone decline: stop proactive contact, not global opt-out.
    if re.fullmatch(r'(?:hola[,.! ]+)?no[, ]+gracias[.! ]*', msg):
        return "CONTACT_DECLINED"
    if re.fullmatch(r'(?:hola[,.! ]+)?no se (?:ni )?que es procasa[.! ]*', msg):
        return "CONTACT_UNRECOGNIZED"
    if re.fullmatch(r'(?:no (?:me|ne) (?:respondiste|respindiste|respondio|contestaste|contesto)|sigo esperando (?:su |tu )?respuesta)[.! ]*', msg):
        return "RESPONSE_PENDING"
    if not re.match(r'no\b', msg) and re.search(r'\b(?:quiero|necesito) vender\b', msg):
        return "MIXED_NEEDS" if re.search(r'\bcomprar\b', msg) else "OWNER_SELL"
    if not re.match(r'no\b', msg) and re.search(r'\b(?:quiero|necesito) (?:arrendar|alquilar) mi\b', msg):
        return "OWNER_RENT"
    for p in parts:
        if re.match(r'(?:no |tampoco |prefiero no |nunca |si )', p):
            continue
        if re.match(r'(?:hago una oferta|quiero ofertar|mi oferta (?:es|seria)|ofrezco \d+|quiero presentar una oferta)\b', p):
            return "GIVE_OFFER"
        if re.match(r'(?:(?:quiero|podemos|podria|puedo) (?:agendar|coordinar|hacer) una visita|(?:podemos|puedo|podria|quiero) (?:ir a )?visitar(?:la|lo)?|(?:cuando|como) (?:puedo|podemos) (?:visitar|ir a ver)|agendemos (?:una )?visita)\b', p):
            return "ASK_VISIT"
        if re.match(r'(?:me (?:puedes|pueden|podrias|podrian) llamar|llamame|llamenme|quiero (?:hablar|conversar) con (?:un |una )?(?:ejecutivo|ejecutiva|asesor|asesora|corredor)|contacteme por telefono)\b', p):
            return "ASK_CONTACT"
    if re.search(r'\bno puedo visitar\b', msg) and re.search(r'\b(?:proxima semana|otro dia|el sabado|el lunes).*\bsi\b', msg):
        return "ASK_VISIT"
    if re.match(r'^(?:si|dale|bueno|perfecto)[, ]', msg) and not re.search(r'\bno\b', msg):
        agent = plain(previous_agent)
        if '?' in previous_agent and not re.search(r'\b(?:no|o)\b', agent):
            if re.search(r'\b(?:visita|visitar)\b', agent):
                return "ASK_VISIT"
            if re.search(r'\b(?:llame|llamar|llamada)\b', agent):
                return "ASK_CONTACT"
    if any(re.search(r'\b(?:tienen (?:otra|otras|algo parecido)|busco (?:otra|otras)|otras? (?:opciones|alternativas)|ahora busco en|mejor en otra comuna)\b', p) and not p.startswith('no ') for p in parts):
        return "ASK_ALTERNATIVES"
    if re.search(r'\b(?:esa|esta|ese|este|la|el) (?:casa|propiedad|opcion|departamento) no me interesa|\bno me interesa (?:esa|esta|ese|este|la|el) (?:casa|propiedad|opcion|departamento)\b', msg):
        return "PROPERTY_DECLINED"
    if any(re.match(r'(?:me interesa|estoy interesad[oa]|sigo interesad[oa])\b', p) for p in parts):
        return "INTERESTED"
    if any(re.match(r'(?:ahora )?(?:busco|necesito (?:una? )?(?:casa|departamento)|quiero comprar|quiero arrendar)\b', p) for p in parts):
        return "SEARCH_UPDATED"
    if re.search(r'\b(?:ahora puedo llegar a|mi presupuesto|presupuesto maximo)\b', msg):
        return "SEARCH_UPDATED"
    if any(re.match(r'(?:cuanto cuesta|cual es el precio|gastos comunes|donde queda|tiene estacionamiento|me das detalles|quiero informacion|cuantos dormitorios|esta disponible)\b', p) for p in parts):
        return "ASK_INFO"
    # Reuse the established office/campaign vocabulary, with stricter
    # executive evidence guards. Do not import its campaign closure policy.
    from .price_drop_campaign_classifier import classify_price_drop_reply
    shared = classify_price_drop_reply(text)
    if not re.search(r'\b(?:no|nunca|tampoco|si|cuando|dijo|dicen)\b', msg):
        if shared.intent in {'ASK_VISIT', 'ASK_CONTACT', 'GIVE_OFFER'}:
            return shared.intent
        if shared.reason == 'explicit_general_interest':
            return 'INTERESTED'
        if shared.alternatives_requested:
            return 'ASK_ALTERNATIVES'
    return "NO_SIGNAL"


def extract(text, previous=None, message_id=None):
    """Explicit needs, separated by customer role; evidence references no PII.

    The legacy search is the demand need only. Owner supply never overwrites it.
    Unknown attributes are absent. No guessed location gazetteer or model data.
    """
    state = deepcopy(previous or {})
    if ambiguous(text):
        return state
    msg = plain(text)
    needs = state.setdefault('needs', {})
    requirement_context = bool(re.search(r'\b(?:busco|necesito|quiero|presupuesto|puedo llegar a|mejor en)\b', msg))
    for part in clauses(text):
        if part.startswith(('no ', 'antes ', 'buscaba ', 'ya compre ', 'ya arrende ')):
            continue
        if re.search(r'\bno\b', part):
            continue  # Mixed positive/negative requirements need review.
        if not requirement_context or re.search(r'\b(?:mi hermana|mi hermano|mi amigo|mi amiga|el dueno|el ejecutivo|mi vecino)\b', part):
            continue
        role = ('OWNER_SELL' if re.search(r'\bvender\b', part) else
                'OWNER_RENT' if re.search(r'\b(?:arrendar|alquilar) mi\b', part) else 'DEMAND')
        existing = needs.get(role, {})
        values = {}
        if re.search(r'\b(?:arriendo|arrendar|alquilar)\b', part):
            values['operation'] = 'ARRIENDO'
        elif re.search(r'\b(?:comprar|compra|venta|vender)\b', part):
            values['operation'] = 'VENTA'
        for word, value in [('departamento|depto', 'DEPARTAMENTO'), ('casa', 'CASA'), ('terreno', 'TERRENO'), ('oficina', 'OFICINA'), ('local comercial', 'LOCAL_COMERCIAL')]:
            if re.search(r'\b(?:' + word + r')\b', part):
                values['property_type'] = value
                break
        # Only location introduced as a search/supply requirement. Stop at
        # conjunctions and feature/budget clauses; do not accept arbitrary LLM names.
        location = re.search(r'\ben ([a-zñ ]+?)(?=,|\b(?:y despues|para|con|de \d|hasta|por|presupuesto)\b|$)', part)
        if location and re.search(r'\b(?:busco|buscar|necesito|comprar|vender|arrendar|arriendo|mejor)\b', part):
            names = [x.strip() for x in re.split(r'\s+o\s+|\s+y\s+', location[1])]
            if all(n in COMMUNE_OFFICIAL_NAMES for n in names):
                values['comunas'] = [COMMUNE_OFFICIAL_NAMES[n] for n in names[:5]]
        for match in re.finditer(r'(?:hasta|maximo(?: de)?|puedo llegar a|presupuesto(?: es| de)?)\s*\$?\s*(\d[\d.]*(?:,\d+)?)\s*(uf|clp|pesos)\b', part):
            amount = float(match[1].replace('.', '').replace(',', '.'))
            key = 'max_price_uf' if match[2] == 'uf' else 'max_price_clp'
            if 0 < amount <= (100000 if key.endswith('uf') else 100000000000):
                values[key] = int(amount) if amount.is_integer() else amount
        price_range = re.search(r'\bentre (\d[\d.]*) y (\d[\d.]*)\s*(uf|clp|pesos)\b', part)
        if price_range:
            lower, upper = (int(price_range[i].replace('.', '')) for i in (1, 2))
            currency = 'uf' if price_range[3] == 'uf' else 'clp'
            if 0 < lower <= upper <= (100000 if currency == 'uf' else 100000000000):
                values['min_price_' + currency] = lower
                values['max_price_' + currency] = upper
        reference = re.search(r'\bcodigo\s+(\d{3,8})\b', part)
        if reference:
            values['property_reference'] = reference[1]
        for noun, key in [('dormitorios?|piezas?|habitaciones?', 'bedrooms'), ('banos?', 'bathrooms'), ('estacionamientos?', 'parking')]:
            feature = re.search(r'\b(\d{1,2})\s*(?:' + noun + r')\b', part)
            if feature and 0 <= int(feature[1]) <= 12:
                values['min_' + key if re.search(r'\b(?:al menos|minimo)\s*' + feature[1], part) else key] = int(feature[1])
        if re.search(r'\bnecesito (?:credito|financiamiento)\b', part):
            values['financing_needed'] = True
        if re.search(r'\b(?:urgente|este mes|antes de fin de mes)\b', part):
            values['urgency'] = 'EXPLICIT'
        if values:
            if 'operation' in values and existing.get('operation') not in (None, values['operation']):
                existing = {}
                state.setdefault('field_evidence', {})[role] = {}
            # Changing currency must not leave two contradictory maxima.
            if 'max_price_uf' in values:
                existing.pop('max_price_clp', None)
            if 'max_price_clp' in values:
                existing.pop('max_price_uf', None)
            existing.update(values)
            needs[role] = existing
            evidence = state.setdefault('field_evidence', {}).setdefault(role, {})
            evidence.update({k: {'status': 'confirmed', 'provider_message_id': message_id} for k in values})
    return state
