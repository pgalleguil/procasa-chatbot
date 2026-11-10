"""Deterministic customer-facing semantic guards for the V1 pipeline."""
from __future__ import annotations

import re

from .conversation_policy import is_specific_property_question
from .property_lookup import get_prop_location, get_prop_operation


def _format_uf(value) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    if amount == int(amount):
        return f"{int(amount):,}".replace(",", ".")
    text = f"{amount:.2f}".rstrip("0").rstrip(".")
    integer, _, decimal = text.partition(".")
    return f"{int(integer):,}".replace(",", ".") + (f",{decimal}" if decimal else "")


def build_specific_property_response(message: str, property_doc: dict | None,
                                     *, price_drop_context: dict | None = None) -> str | None:
    """Answer any verified public property question, not only GC/orientation.

    New leads and price-drop replies share this reader. A strict public-field
    allowlist blocks exact addresses, owner details and free-text notes.
    """
    from .public_property_facts import answer_public_property_question
    return answer_public_property_question(
        message, property_doc, campaign_context=price_drop_context,
    )



def use_natural_verified_property_reply(message: str, model_reply: str,
                                        property_doc: dict | None,
                                        *, campaign_context: dict | None = None) -> bool:
    """Keep the commercial LLM voice when its requested facts agree with Prop360.

    The deterministic reply remains the fallback for unverified values,
    technical full fichas, private details, price-drop history and negotiation.
    Never infer a price-drop campaign from the property's current price.
    """
    import unicodedata
    from .public_property_facts import (
        requested_public_fact_keys, public_property_facts,
        prefers_natural_property_overview,
    )

    def normal(text):
        value = unicodedata.normalize("NFKD", str(text or "").casefold())
        return "".join(c for c in value if not unicodedata.combining(c))

    question = normal(message)
    answer = normal(model_reply)
    if not property_doc or not answer or prefers_natural_property_overview(message):
        return False
    # A commune/price mentioned in an alternatives request is a search filter,
    # not a question about the listing attached to the campaign.
    from .conversation_policy import alternative_requested
    if alternative_requested(message):
        return False
    # These cases need the deterministic public-fact/privacy/commercial guard.
    if re.search(
        r"\b(?:ficha\s+(?:tecnica|completa)|(?:en|como)\s+(?:formato\s+)?"
        r"(?:lista|tabla)|rebaj[ao]|antes\s+cuanto|negoci|contraoferta|"
        r"dueno|propietario|direccion\s+exacta|telefono\s+del|rut\s+del)\b",
        question,
    ):
        return False
    keys = requested_public_fact_keys(message)
    if not keys:
        return False
    facts = public_property_facts(property_doc)
    if any(facts.get(key) in (None, "", "No informado") for key in keys):
        return False
    # No campaign delivered to this lead: never allow a spontaneous claim of
    # an earlier price or price-drop notification in a normal property answer.
    if not campaign_context and re.search(
        r"\b(?:rebaj\w*|antes costaba|bajo de precio|bajo\s+\d+\s*(?:uf|pesos?)|bajaron el precio)\b", answer,
    ):
        return False

    # The LLM must repeat every requested verified value. Never replace a
    # contradictory or incomplete answer with a persuasive but inaccurate one.
    for key in keys:
        expected = normal(facts[key])
        if key == "precio":
            expected = expected.split(" (referencia")[0]
        if key in {"precio", "gastos"}:
            # Chilean prices may be written as 2.690, 2690, $85.000 or 85000.
            value_digits = re.sub(r"\D", "", expected)
            mentioned_numbers = {
                re.sub(r"\D", "", token)
                for token in re.findall(r"(?<!\d)\d[\d., ]*(?!\d)", answer)
            }
            if value_digits not in mentioned_numbers:
                return False
        elif key in {"dormitorios", "banos", "bodegas", "estacionamientos", "ascensores"}:
            field_terms = {
                "dormitorios": r"dormitorios?|habitaciones?|piezas?",
                "banos": r"banos?",
                "bodegas": r"bodegas?",
                "estacionamientos": r"estacionamientos?|parking",
                "ascensores": r"ascensores?|elevadores?",
            }[key]
            number = re.escape(expected)
            # A matching number elsewhere in the property description does
            # not prove that the model answered the requested field.
            if not re.search(
                rf"(?:\b{number}\b.{{0,24}}\b(?:{field_terms})\b|"
                rf"\b(?:{field_terms})\b.{{0,24}}\b{number}\b)",
                answer,
            ):
                return False
        elif key == "equipamiento":
            amenities = [normal(x).strip() for x in expected.split(",")]
            if not amenities or not any(x and x in answer for x in amenities):
                return False
        elif key == "disponible":
            if not re.search(r"\b(?:disponib|activ|confirmar)\w*", answer):
                return False
        elif key == "fotos":
            if "procasa.cl/" not in answer:
                return False
        else:
            clean_expected = re.sub(r"[^a-z0-9]", "", expected)
            clean_answer = re.sub(r"[^a-z0-9]", "", answer)
            if not clean_expected or clean_expected not in clean_answer:
                return False
    return True


def use_natural_verified_alternative_reply(
    message: str,
    model_reply: str,
    *,
    requested_communes: list[str] | tuple[str, ...] = (),
    original_property_code: str | None = None,
    verified_result_codes: list[str] | tuple[str, ...] = (),
    campaign_context: dict | None = None,
    property_doc: dict | None = None,
) -> bool:
    """Keep a conversational no-results reply only when it answers the search.

    This is intentionally limited to verified empty RAG searches. Positive
    search results continue through the deterministic listing renderer so a
    model cannot invent a property, price, or link.
    """
    import unicodedata
    from .conversation_policy import alternative_requested

    if not alternative_requested(message):
        return False
    answer = unicodedata.normalize("NFKD", str(model_reply or "").casefold())
    answer = "".join(char for char in answer if not unicodedata.combining(char))
    if not answer.strip() or len(answer.split()) < 5:
        return False
    if re.fullmatch(r"\s*(?:precio|comuna|disponibilidad)\s*:\s*[^\n]+\s*", answer):
        return False
    if not re.search(
        r"\b(?:busqu\w*|busc\w*|revis\w*|opciones?|alternativas?|"
        r"coincidencias?|resultados?|encontr\w*|ampli\w*)\b",
        answer,
    ):
        return False
    if re.search(
        r"\b(?:direcci[oó]n\s+exacta|calle\s+[a-z0-9]+|n[uú]mero\s+del\s+departamento|"
        r"rut\s+del\s+(?:propietario|due[nñ]o)|tel[eé]fono\s+del\s+(?:propietario|due[nñ]o)|"
        r"nombre\s+del\s+(?:propietario|due[nñ]o))\b",
        answer,
    ):
        return False

    for commune in requested_communes:
        normalized_commune = unicodedata.normalize("NFKD", str(commune or "").casefold())
        normalized_commune = "".join(
            char for char in normalized_commune if not unicodedata.combining(char)
        )
        if normalized_commune and normalized_commune not in answer:
            return False

    # An empty RAG result cannot support claims that a new listing was found.
    has_no_match = bool(re.search(
        r"\b(?:no\s+(?:me\s+)?(?:aparec\w*|encontr\w*|hay|teng\w*|exist\w*)|"
        r"sin\s+coincidencias?|ningun\w*\s+(?:opcion|propiedad|resultado))\b",
        answer,
    ))
    claims_match = bool(re.search(
        r"\b(?:encontr[eé]\s+(?:estas?\s+)?(?:opciones?|propiedades?)|"
        r"hay\s+(?:opciones?|propiedades?)|tengo\s+(?:opciones?|propiedades?)\s+disponibles?)\b",
        answer,
    ))
    if claims_match and not has_no_match and not verified_result_codes:
        return False
    if not has_no_match and not verified_result_codes:
        return False

    allowed_codes = {
        str(code).strip() for code in (*verified_result_codes, original_property_code)
        if code not in (None, "")
    }
    mentioned_codes = set(re.findall(
        r"\b(?:c[oó]digo|propiedad)\s*(?:n[uú]mero\s*)?[:#]?\s*(\d{4,6})\b",
        answer,
        re.IGNORECASE,
    ))
    if not mentioned_codes.issubset(allowed_codes):
        return False
    if not verified_result_codes and re.search(r"https?://|www\.", answer):
        return False

    # Monetary claims about the campaign property still have to match its
    # recorded price event. Search relevance does not relax price safeguards.
    context = campaign_context or {}
    currency = str(context.get("currency") or "").upper()
    if currency in {"UF", "CLP"}:
        if currency == "UF":
            old, new = context.get("old_price_uf"), context.get("new_price_uf")
            pattern = r"\bUF\s*([0-9][0-9.,]*)"
        else:
            old, new = context.get("old_price_clp"), context.get("new_price_clp")
            pattern = r"\$\s*([0-9][0-9.,]*)|\b([0-9][0-9.,]*)\s*(?:pesos?|CLP)\b"
        monetary_values = []
        for match in re.finditer(pattern, answer, re.IGNORECASE):
            token = next((group for group in match.groups() if group), "")
            token = token.replace(" ", "")
            if token.count(",") == 1 and len(token.rsplit(",", 1)[1]) in (1, 2):
                integer, decimal = token.rsplit(",", 1)
                integer = integer.replace(".", "")
                try:
                    monetary_values.append(float(f"{integer}.{decimal}"))
                except ValueError:
                    return False
            else:
                try:
                    monetary_values.append(float(token.replace(".", "").replace(",", "")))
                except ValueError:
                    return False
        if monetary_values:
            try:
                allowed_prices = {float(old), float(new), float(old) - float(new)}
            except (TypeError, ValueError):
                return False
            if any(value not in allowed_prices for value in monetary_values):
                return False

    return True


def rag_customer_response(properties: list[dict] | None, *, criteria: dict | None = None) -> str:
    properties = list(properties or [])[:2]
    if not properties:
        criteria = criteria or {}
        communes = criteria.get("comunas") or criteria.get("comuna") or []
        if isinstance(communes, str):
            communes = [part.strip() for part in re.split(r"[,;/]", communes) if part.strip()]
        communes = list(dict.fromkeys(str(value).strip() for value in communes if str(value).strip()))
        if communes:
            place = ", ".join(communes)
            return (
                f"Busqué propiedades en {place} con esos criterios y no encontré "
                "coincidencias disponibles por ahora. Si quieres, puedo ampliar "
                "la búsqueda o ajustar algún criterio."
            )
        return "No encontré coincidencias exactas con esos criterios. Si quieres, podemos ampliar un poco la búsqueda."
    lines = ["Encontré estas opciones que coinciden con lo que buscas:"]
    for prop in properties:
        location = get_prop_location(prop)
        operation = get_prop_operation(prop)
        tipo = operation.get("tipo") or "Propiedad"
        comuna = location.get("comuna") or location.get("sector") or "ubicación por confirmar"
        sector = location.get("sector")
        place = f"{comuna} · {sector}" if sector and sector.casefold() != comuna.casefold() else comuna
        if operation.get("precio_uf") not in (None, ""):
            price = f"{_format_uf(operation['precio_uf'])} UF"
        elif operation.get("precio_clp") not in (None, ""):
            price = f"${operation['precio_clp']}"
        else:
            price = "precio por confirmar"
        characteristics = prop.get("caracteristicas") or {}
        bedrooms = characteristics.get("dormitorios") or prop.get("dormitorios")
        surface = characteristics.get("superficie_util") or prop.get("m2_utiles")
        useful = (f"{bedrooms} dormitorio(s)" if bedrooms not in (None, "") else
                  f"{surface} m² útiles" if surface not in (None, "") else
                  (f"{characteristics.get('banos')} baño(s)" if characteristics.get("banos") not in (None, "") else "ficha por revisar"))
        publications = prop.get("publicaciones") or {}
        procasa = publications.get("procasa") if isinstance(publications, dict) else {}
        link = ((procasa or {}).get("url_procasa") if isinstance(procasa, dict) else None)
        link = link or prop.get("url_procasa") or f"https://www.procasa.cl/{prop.get('codigo')}"
        code = str(prop.get("codigo") or "").strip()
        code_label = f"Código {code}; " if code else ""
        lines.append(f"• {code_label}{tipo} en {place} ({operation.get('operacion') or 'operación por confirmar'}), {price}; {useful}.\n  {link}")
    lines.append("¿Cuál te gustaría revisar?")
    return "\n".join(lines)


def new_link_template_contaminated(response: str) -> bool:
    normalized = str(response or "").casefold()
    return bool(re.search(r"\b(?:no\s+descarto|dejamos\s+activa|propiedad\s+descartada|no\s+me\s+gust(?:a|o)|no\s+qued[oó]\s+conforme)\b", normalized))


def new_link_context_response(property_doc: dict, external_id: str | None = None) -> str:
    location = get_prop_location(property_doc)
    operation = get_prop_operation(property_doc)
    identity = str(property_doc.get("codigo") or external_id or "la publicación")
    place = location.get("comuna") or location.get("sector") or "la ubicación indicada"
    detail = f"{operation.get('tipo') or 'propiedad'} en {place}"
    if operation.get("operacion"):
        detail += f" para {operation['operacion'].casefold()}"
    return f"Gracias por compartir el enlace. Identifiqué la propiedad {identity}: {detail}. Tomaré esta propiedad como el nuevo contexto para revisar sus antecedentes."


def evaluate_customer_response(customer_message: str, response: str, *,
                               rag_results: list[dict] | None = None,
                               resolved_property: dict | None = None) -> dict:
    """Semantic evaluator used by the V1 replay gate.

    Internal state is not enough to pass: the customer-facing text must answer
    the asked field, present concrete RAG results when available, and avoid a
    previous-property rejection template after a new link.
    """
    message = str(customer_message or "")
    text = str(response or "")
    normalized = text.casefold()
    checks = {
        "ANSWER_RELEVANCE": True,
        "QUESTION_ACTUALLY_ANSWERED": True,
        "NO_TEMPLATE_CONTAMINATION": True,
        "RAG_RESULTS_ACTUALLY_PRESENTED": True,
    }
    if re.search(r"\bmascot(?:a|as)\b", message.casefold()):
        checks["QUESTION_ACTUALLY_ANSWERED"] = bool(
            re.search(r"mascot(?:a|as)", normalized)
            and any(marker in normalized for marker in ("indica", "acept", "admit", "no indica"))
        )
        checks["ANSWER_RELEVANCE"] = checks["QUESTION_ACTUALLY_ANSWERED"]
    if rag_results:
        identifiers = {str(item.get("codigo") or "").casefold() for item in rag_results[:2]}
        links = {f"https://www.procasa.cl/{code}" for code in identifiers if code}
        checks["RAG_RESULTS_ACTUALLY_PRESENTED"] = any(
            token and (token in normalized or token in text)
            for token in (*identifiers, *links)
        )
        checks["ANSWER_RELEVANCE"] = checks["ANSWER_RELEVANCE"] and checks["RAG_RESULTS_ACTUALLY_PRESENTED"]
    if resolved_property and re.search(r"https?://|www\.", message):
        checks["NO_TEMPLATE_CONTAMINATION"] = not new_link_template_contaminated(text)
        checks["ANSWER_RELEVANCE"] = checks["ANSWER_RELEVANCE"] and checks["NO_TEMPLATE_CONTAMINATION"]
    return {**checks, "PASS": all(checks.values())}


def evaluate_price_drop_final_response(
    customer_message: str,
    response: str,
    classification: dict | None = None,
    *,
    original_commune: str | None = None,
    requested_communes: list[str] | tuple[str, ...] = (),
    property_doc: dict | None = None,
) -> dict:
    """Check campaign semantics on the text actually delivered to the client."""
    import unicodedata
    from .public_property_facts import public_property_facts, requested_public_fact_keys

    def normalized(value):
        folded = unicodedata.normalize("NFKD", str(value or "").casefold())
        return "".join(char for char in folded if not unicodedata.combining(char))

    message = normalized(customer_message)
    final = str(response or "").strip()
    answer = normalized(final)
    classification = classification or {}
    is_opt_out = classification.get("label") == "BAJA"
    checks = {
        "FINAL_RESPONSE_PRESENT": not final if is_opt_out else bool(final),
        "ALTERNATIVE_REQUEST_ANSWERED": True,
        "ALTERNATIVE_TARGETS_ADDRESSED": True,
        "NO_ORIGINAL_COMMUNE_SUBSTITUTION": True,
        "OFFER_HANDLER_ANSWERED": True,
        "NO_CALL_PREFERENCE_RESPECTED": True,
        "VISIT_NOT_FALSELY_CONFIRMED": True,
        "REQUESTED_FACT_ANSWERED": True,
        "MISSING_FACT_ACKNOWLEDGED": True,
        "GLOBAL_OPT_OUT_SUPPRESSED": True,
    }

    if classification.get("alternatives_requested"):
        relevant = bool(re.search(
            r"\b(?:busqu\w*|busc\w*|revis\w*|opciones?|alternativas?|"
            r"coincidencias?|resultados?|encontr\w*|ampli\w*)\b",
            answer,
        ))
        checks["ALTERNATIVE_REQUEST_ANSWERED"] = relevant
        normalized_targets = [normalized(value) for value in requested_communes if value]
        checks["ALTERNATIVE_TARGETS_ADDRESSED"] = all(
            target in answer for target in normalized_targets
        )
        original = normalized(original_commune)
        requested_elsewhere = any(target != original for target in normalized_targets)
        direct_original_field = bool(original and re.search(
            rf"\bcomuna\s*:\s*{re.escape(original)}\b", answer,
        ))
        checks["NO_ORIGINAL_COMMUNE_SUBSTITUTION"] = not (
            requested_elsewhere and direct_original_field
        )

    if classification.get("offer_intent") or classification.get("intent") == "GIVE_OFFER":
        identifies_handler = bool(re.search(
            r"\b(?:administraci[oó]n|propietari[oa]|equipo\s+comercial)\b", answer,
        ))
        reviews_offer = bool(re.search(
            r"\b(?:administraci[oó]n|propietari[oa]|equipo\s+comercial)\b.{0,55}\b"
            r"(?:revis\w*|eval[uú]\w*|gestion\w*)\b.{0,35}\bofert\w*\b|"
            r"\bofert\w*\b.{0,45}\b(?:revis\w*|eval[uú]\w*|gestion\w*)\b.{0,35}\b"
            r"(?:administraci[oó]n|propietari[oa]|equipo\s+comercial)\b",
            answer,
        ))
        customer_submission_route = bool(re.search(
            r"\b(?:puedes|podr[ií]as|comparte|env[ií]a|presenta|deja)\b.{0,50}\b"
            r"(?:por\s+este\s+chat|monto|condiciones|oferta|solicitud)\b|"
            r"\bpor\s+este\s+chat\b.{0,55}\b(?:monto|condiciones|oferta|solicitud)\b",
            answer,
        ))
        unauthorized_executive_promise = bool(re.search(
            r"\b(?:ejecutiv[oa]|asesor[oa])\b.{0,90}\b"
            r"(?:te\s+contact\w*|te\s+llam\w*|desde\s+su\s+n[uú]mero\s+directo)\b",
            answer,
        ))
        checks["OFFER_HANDLER_ANSWERED"] = (
            identifies_handler and reviews_offer and customer_submission_route
            and not unauthorized_executive_promise
        )

    if classification.get("do_not_call"):
        clauses = re.split(r"\b(?:pero|aunque|sin embargo)\b|[.!?;,\n]+", answer)
        solicits_call = any(
            re.search(
                r"\b(?:te\s+(?:pued\w*|van\s+a|vamos\s+a|llamar\w*|contact\w*\s+por\s+tel[eé]fono)|"
                r"(?:quieres?|prefieres?)\s+que\s+(?:.{0,35}\s+)?te\s+llam\w*|"
                r"coordinar\s+(?:una\s+)?llamada)\b",
                clause,
            )
            and not re.search(r"\b(?:no|nunca|jam[aá]s)\b.{0,30}\b(?:llam\w*|contact\w*)\b", clause)
            for clause in clauses
        )
        checks["NO_CALL_PREFERENCE_RESPECTED"] = not solicits_call

    if classification.get("visit_requested"):
        confirmed_claim = bool(re.search(
            r"\b(?:visita|horario)\b.{0,50}\b(?:confirmad[ao]|agendad[ao]|reservad[ao])\b|"
            r"\b(?:te\s+esperamos|ya\s+qued[oó]\s+reservad[ao])\b",
            answer,
        ))
        negated_confirmation = bool(re.search(
            r"\b(?:no\s+(?:est[aá]\s+)?(?:confirmad[ao]|agendad[ao]|reservad[ao])|"
            r"todav[ií]a\s+no\s+est[aá]\s+confirmad[ao])\b",
            answer,
        ))
        checks["VISIT_NOT_FALSELY_CONFIRMED"] = not (confirmed_claim and not negated_confirmation)

    if is_opt_out:
        checks["GLOBAL_OPT_OUT_SUPPRESSED"] = not final

    if property_doc:
        from .public_property_facts import public_property_facts
        facts = public_property_facts(property_doc)
        keys = requested_public_fact_keys(customer_message)
        field_terms = {
            "precio": r"precio|valor|uf|\$",
            "gastos": r"gastos\s+comunes|ggcc",
            "dormitorios": r"dormitorios?|habitaciones?|piezas?",
            "banos": r"ba[nñ]os?",
            "util": r"superficie\s+util|metros?\s+utiles?|m2\s+utiles?",
            "total": r"superficie\s+total|metros?\s+totales?|metraje",
            "comuna": r"comuna|ubicaci[oó]n",
            "disponible": r"disponib|activ|confirmar",
            "orientacion": r"orientaci[oó]n|norte|sur|oriente|poniente",
            "estacionamientos": r"estacionamiento|parking",
            "bodegas": r"bodega",
            "fotos": r"foto|procasa\.cl",
            "mascotas": r"mascota|perro|gato",
        }
        unknown_requested = [
            key for key in keys
            if facts.get(key) in (None, "", "No informado")
        ]
        checks["REQUESTED_FACT_ANSWERED"] = all(
            bool(re.search(field_terms.get(key, re.escape(key)), answer))
            for key in keys if key not in unknown_requested
        )
        checks["MISSING_FACT_ACKNOWLEDGED"] = (
            not unknown_requested
            or bool(re.search(
                r"\b(?:no\s+(?:se\s+)?(?:informa|indica|especifica|aparece)|"
                r"no\s+(?:est[aá]\s+)?informad\w*|por\s+confirmar|"
                r"hay\s+que\s+confirmar|debo\s+confirmar)\b",
                answer,
            ))
        )

    return {**checks, "PASS": all(checks.values())}
