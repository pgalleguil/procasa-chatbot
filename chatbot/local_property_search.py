"""Offline property retrieval. No CRM writes, conversation reads or network by default.

Input: canonical catalog rows and manually supplied structured search profiles.
Optional multilingual embeddings are loaded only by the explicit local CLI.
"""
from __future__ import annotations

from collections import Counter
import math
import re
import unicodedata
from datetime import datetime, timezone

ATTRIBUTES = ("bedrooms", "bathrooms", "parking", "usable_surface_m2", "total_surface_m2", "storage")
BROKERS = {"CORREDOR_SEGURO", "CORREDOR_PROBABLE", "BROKER_CONFIRMED"}
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_CHILEAN_PHONE = re.compile(
    r"(?<!\d)(?:\+?56[\s().-]*)?(?:9[\s().-]*\d{4}[\s().-]?\d{4}|"
    r"2[\s().-]*\d{4}[\s().-]?\d{4})(?!\d)"
)
_PRIVATE_FIELD = re.compile(r"(?:owner|contact|email|e-mail|mail|phone|telephone|telefono|teléfono|whatsapp|conversation|lead|crm|agent_name)", re.I)


def fold(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value or "").casefold())
                   if not unicodedata.combining(c)).strip()

def public_text(value):
    """Remove direct contact details before indexing or returning public text."""
    text = str(value or "")
    text = _EMAIL.sub(" ", text)
    text = _CHILEAN_PHONE.sub(" ", text)
    return " ".join(text.split())


def public_url(value):
    if not isinstance(value, str) or _EMAIL.search(value) or _CHILEAN_PHONE.search(value):
        return None
    return value


def public_value(value):
    """Sanitize nested feature values before they are exposed to local output."""
    if isinstance(value, str):
        return public_text(value)
    if isinstance(value, dict):
        return {public_text(key): public_value(item) for key, item in value.items()
                if not _PRIVATE_FIELD.search(str(key))}
    if isinstance(value, (list, tuple, set)):
        return [public_value(item) for item in value]
    return value


def tokens(text):
    return re.findall(r"[a-z0-9]+", fold(public_text(text)))

def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None

def normalized_price_clp(row):
    """Return an explicit CLP value or a snapshot-rate UF conversion."""
    clp = number(row.get("price_clp"))
    if clp is not None and clp > 0:
        return int(round(clp))
    if str(row.get("price_currency") or "").strip().upper() != "UF":
        return None
    uf = number(row.get("price_uf"))
    rate = number(row.get("price_conversion_uf_clp"))
    if uf is None or uf <= 0 or rate is None or rate <= 0:
        return None
    return int(round(uf * rate))

def property_text(row):
    attrs = row.get("attributes") or {}
    features = public_value(row.get("features") or [])
    if isinstance(features, dict):
        features = " ".join(f"{k} {v}" for k, v in features.items())
    elif isinstance(features, (list, tuple, set)):
        features = " ".join(str(v) for v in features)
    return " ".join(public_text(v) for v in (
        row.get("property_type"), row.get("commune"), row.get("title"),
        row.get("description"), row.get("search_text"),
        features, " ".join(f"{k} {attrs[k]}" for k in ATTRIBUTES if k in attrs)
    ) if v)

def profile_text(profile):
    # Hard filters already enforce commune/type; rank by actual preferences.
    return str(profile.get("query") or "").strip()

def _recent(value, hours, now):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    age = (now - value).total_seconds()
    return 0 <= age <= hours * 3600


def _feature_text_evidence(text, term):
    """Interpret explicit mentions conservatively, including nearby negation."""
    phrase = fold(term)
    if not phrase:
        return None
    pattern = re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)")
    positive = negative = False
    negation = re.compile(
        r"(?:\bno\b|\bsin\b|\bningun[oa]?\b|\bcarece de\b|"
        r"\bno (?:tiene|incluye|posee|cuenta con)\b|\bsin acceso a\b)"
        r"(?:\s+[\w]+){0,3}\s*$"
    )
    normalized_text = fold(text)
    for match in pattern.finditer(normalized_text):
        prefix = normalized_text[max(0, match.start() - 70):match.start()]
        if negation.search(prefix):
            negative = True
        else:
            positive = True
    if positive and negative:
        return None
    if positive:
        return True
    if negative:
        return False
    return None


def _structured_feature(row, term):
    """Return a known boolean feature value, or None when evidence is absent."""
    wanted = fold(term)
    false_values = {"0", "false", "no", "ninguno", "ninguna", "sin", "no tiene", "no incluye"}
    true_values = {"1", "true", "si", "yes", "tiene", "incluye", "incluido", "incluida"}
    for source in (row.get("attributes") or {}, public_value(row.get("features") or {})):
        if isinstance(source, dict):
            for key, value in source.items():
                if fold(key) != wanted:
                    continue
                if isinstance(value, bool):
                    return value
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if math.isfinite(value):
                        return value > 0
                normalized = fold(value)
                if normalized in false_values:
                    return False
                if normalized in true_values:
                    return True
                if value is not None and normalized:
                    return _feature_text_evidence(normalized, wanted)
        elif isinstance(source, (list, tuple, set)):
            for item in source:
                normalized = fold(item)
                if normalized == wanted:
                    return True
                if normalized:
                    status = _feature_text_evidence(normalized, wanted)
                    if status is not None:
                        return status
    return None


def _feature_evidence(row, term):
    known = _structured_feature(row, term)
    if known is not None:
        return known
    return _feature_text_evidence(property_text(row), term)


def _feature_reason(prefix, term):
    suffix = re.sub(r"[^A-Z0-9]+", "_", fold(term).upper()).strip("_")
    return prefix + suffix


def filter_property(profile, row, now):
    """Hard mismatches are excluded; unknown evidence stays in review only."""
    if row.get("human_veto") or row.get("classification_state") in BROKERS:
        return "excluded", ["COMMERCIAL_VETO"]
    if row.get("published") is False or row.get("operational_state") in {"REMOVED", "NOT_AVAILABLE"}:
        return "excluded", ["NOT_AVAILABLE"]
    if fold(row.get("operation")) != fold(profile["operation"]):
        return "excluded", ["OPERATION"]
    if fold(row.get("commune")) not in {fold(x) for x in profile["comunas"]}:
        return "excluded", ["COMMUNE"]
    if fold(row.get("property_type")) not in {fold(x) for x in profile["property_types"]}:
        return "excluded", ["TYPE"]
    if str(row.get("property_code") or "") in {str(x) for x in profile.get("excluded_property_codes", [])}:
        return "excluded", ["BUYER_EXCLUDED"]
    missing = []
    price = normalized_price_clp(row)
    if not price:
        missing.append("PRICE_UNKNOWN")
    elif row.get("price_valid") is not True:
        missing.append("PRICE_UNVERIFIED")
    elif not _recent(row.get("last_price_verified_at"), 48, now):
        missing.append("PRICE_STALE")
    elif price > profile["max_price_clp"]:
        return "excluded", ["OVER_BUDGET"]
    for key, minimum in (profile.get("minimum_attributes") or {}).items():
        actual = number((row.get("attributes") or {}).get(key))
        if actual is None:
            missing.append("UNKNOWN_" + key.upper())
        elif actual < minimum:
            return "excluded", ["MINIMUM_" + key.upper()]
    for key, maximum in (profile.get("maximum_attributes") or {}).items():
        actual = number((row.get("attributes") or {}).get(key))
        if actual is None:
            missing.append("UNKNOWN_" + key.upper())
        elif actual > maximum:
            return "excluded", ["MAXIMUM_" + key.upper()]
    for term in profile.get("required_features", []):
        evidence = _feature_evidence(row, term)
        if evidence is False:
            return "excluded", [_feature_reason("REQUIRED_FEATURE_", term)]
        if evidence is None:
            missing.append(_feature_reason("UNKNOWN_REQUIRED_FEATURE_", term))
    for term in profile.get("excluded_features", []):
        evidence = _feature_evidence(row, term)
        if evidence is True:
            return "excluded", [_feature_reason("FORBIDDEN_FEATURE_", term)]
        if evidence is None:
            missing.append(_feature_reason("UNKNOWN_FORBIDDEN_FEATURE_", term))
    if row.get("published") is not True:
        missing.append("PUBLICATION_STATUS_UNKNOWN")
    if profile.get("location_targets") or profile.get("verified_location"):
        target = profile.get("verified_location") or {}
        geo = row.get("geo") or {}
        def coordinates(obj):
            lat, lon = obj.get("latitude"), obj.get("longitude")
            if (obj.get("verified") is True and all(isinstance(v, (int, float)) and
                    not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon))
                    and -90 <= lat <= 90 and -180 <= lon <= 180):
                return lat, lon
            return None
        a, b = coordinates(target), coordinates(geo)
        radius = number(target.get("radius_m"))
        if not a or not b or not radius:
            missing.append("LOCATION_REVIEW_REQUIRED")
        else:
            lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
            h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
            distance = 6371000 * 2 * math.asin(math.sqrt(min(1, max(0, h))))
            if distance > radius:
                return "excluded", ["OUTSIDE_RADIUS"]
    if row.get("source_type") != "PROCASA":
        missing.append("EXTERNAL_HUMAN_VERIFICATION")
    else:
        if row.get("operational_state") != "ACTIVE":
            missing.append("AVAILABILITY_UNKNOWN")
        for field in ("last_price_verified_at", "last_availability_verified_at"):
            if not _recent(row.get(field), 48, now):
                missing.append(field.upper() + "_STALE")
    return ("review", missing) if missing else ("compatible", [])

def validate_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    if not isinstance(profile.get("query", ""), str):
        raise ValueError("query must be text")
    if not isinstance(profile.get("minimum_attributes", {}), dict):
        raise ValueError("minimum_attributes must be an object")
    if not isinstance(profile.get("maximum_attributes", {}), dict):
        raise ValueError("maximum_attributes must be an object")
    if fold(profile.get("operation")) not in {"venta", "arriendo"}:
        raise ValueError("operation must be VENTA or ARRIENDO")
    for key in ("comunas", "property_types"):
        if not isinstance(profile.get(key), list) or not profile[key] or not all(isinstance(x, str) and x.strip() for x in profile[key]):
            raise ValueError("missing or invalid " + key)
    if not number(profile.get("max_price_clp")):
        raise ValueError("positive max_price_clp required")
    for key, value in (profile.get("minimum_attributes") or {}).items():
        if key not in ATTRIBUTES or number(value) is None:
            raise ValueError("invalid minimum attribute: " + key)
    for key, value in (profile.get("maximum_attributes") or {}).items():
        if key not in ATTRIBUTES or number(value) is None:
            raise ValueError("invalid maximum attribute: " + key)
    for key in ("required_features", "excluded_features"):
        values = profile.get(key, [])
        if not isinstance(values, list) or not all(isinstance(x, str) and x.strip() for x in values):
            raise ValueError("invalid " + key)
    for key, minimum in (profile.get("minimum_attributes") or {}).items():
        maximum = (profile.get("maximum_attributes") or {}).get(key)
        if maximum is not None and minimum > maximum:
            raise ValueError("minimum exceeds maximum for " + key)

class LocalPropertyIndex:
    def __init__(self, rows, embedder=None, *, cache=None, reranker=None, rerank_pool=50):
        self.rows = list(rows)
        keys = [r.get("listing_key") for r in self.rows]
        if any(not isinstance(k, str) or not k for k in keys) or len(set(keys)) != len(keys):
            raise ValueError("unique listing_key required")
        self.texts = [property_text(r) for r in self.rows]
        self.counts = [Counter(tokens(t)) for t in self.texts]
        self.lengths = [sum(c.values()) for c in self.counts]
        self.avglen = max(1, sum(self.lengths) / max(1, len(self.rows)))
        self.df = Counter(word for counts in self.counts for word in counts)
        self.embedder = embedder
        self.reranker = reranker
        if not isinstance(rerank_pool, int) or not 1 <= rerank_pool <= 500:
            raise ValueError("rerank_pool must be 1..500")
        self.rerank_pool = rerank_pool
        self.text_by_key = dict(zip(keys, self.texts))
        self.vectors = None
        if embedder and self.rows:
            self.vectors = (cache.load_or_encode(keys, self.texts, embedder)
                            if cache else embedder.encode(self.texts))

    def _bm25(self, i, terms):
        value = 0.
        for word in terms:
            tf = self.counts[i][word]
            if tf:
                df = self.df[word]
                idf = math.log(1 + (len(self.rows) - df + .5) / (df + .5))
                value += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * self.lengths[i] / self.avglen))
        return value

    def search(self, profile, limit=10, now=None):
        validate_profile(profile)
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500")
        now = now or datetime.now(timezone.utc)
        terms = set(tokens(profile_text(profile)))
        query_vector = self.embedder.encode([profile_text(profile)])[0] if self.vectors is not None and terms else None
        eligible = []
        rejected = Counter()
        for i, row in enumerate(self.rows):
            state, reasons = filter_property(profile, row, now)
            if state == "excluded":
                rejected.update(reasons)
                continue
            lexical = self._bm25(i, terms)
            dense = None
            if query_vector is not None:
                # CLI provides unit-normalized vectors.
                dense = float(self.vectors[i] @ query_vector)
                if not math.isfinite(dense):
                    raise ValueError("nonfinite embedding similarity")
            eligible.append((i, state, reasons, lexical, dense))
        # Reciprocal rank fusion: no incompatible listing can win on similarity.
        lex_order = sorted((x for x in eligible if x[3] > 0), key=lambda x: (-x[3], self.rows[x[0]]["listing_key"]))
        lex_rank = {x[0]: rank for rank, x in enumerate(lex_order, 1)}
        dense_order = sorted((x for x in eligible if x[4] is not None), key=lambda x: (-x[4], self.rows[x[0]]["listing_key"]))
        dense_rank = {x[0]: rank for rank, x in enumerate(dense_order, 1)}
        groups = {"compatible": [], "review": []}
        for i, state, reasons, lexical, dense in eligible:
            row = self.rows[i]
            score = (1 / (60 + lex_rank[i]) if i in lex_rank else 0) + (1 / (60 + dense_rank[i]) if i in dense_rank else 0)
            groups[state].append({"listing_key": row["listing_key"], "source_type": row.get("source_type"),
                "url": public_url(row.get("canonical_url")), "price_clp": normalized_price_clp(row),
                "price_uf": row.get("price_uf"), "price_currency": row.get("price_currency"),
                "price_conversion_date": row.get("price_conversion_date"),
                "price_conversion_uf_clp": row.get("price_conversion_uf_clp"),
                "attributes": {key: number((row.get("attributes") or {}).get(key))
                               for key in ATTRIBUTES
                               if number((row.get("attributes") or {}).get(key)) is not None},
                "commune": row.get("commune"),
                "property_type": row.get("property_type"), "title": public_text(row.get("title")),
                "description_excerpt": public_text(row.get("description"))[:240],
                "features": public_value(row.get("features") or {}),
                "source_portal": public_text(row.get("source_portal")),
                "source_updated_at": row.get("source_updated_at"),
                "score": score, "bm25": lexical, "cosine": dense, "review_reasons": reasons})
        for group in groups.values():
            group.sort(key=lambda x: (-x["score"], x["price_clp"] if number(x["price_clp"]) else math.inf, x["listing_key"]))
            if self.reranker and group and terms:
                # Rerank separately within each safety tier. Never promote
                # an unverified row to compatible or reintroduce exclusions.
                head = group[:max(limit, self.rerank_pool)]
                scores = list(self.reranker.score(profile_text(profile),
                              [self.text_by_key[x["listing_key"]] for x in head]))
                if len(scores) != len(head) or any(not math.isfinite(float(s)) for s in scores):
                    raise ValueError("invalid reranker scores")
                for item, score in zip(head, scores):
                    item["reranker_score"] = float(score)
                head.sort(key=lambda x: (-x["reranker_score"], -x["score"], x["listing_key"]))
                group[:len(head)] = head
        return {"mode": "hybrid" if query_vector is not None else "lexical",
                "reranked": self.reranker is not None and bool(terms),
                "scanned": len(self.rows), "excluded": dict(rejected),
                "compatible_count": len(groups["compatible"]), "review_count": len(groups["review"]),
                "compatible": groups["compatible"][:limit], "review": groups["review"][:limit]}
