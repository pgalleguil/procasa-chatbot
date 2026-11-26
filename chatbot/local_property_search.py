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
def fold(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value or "").casefold())
                   if not unicodedata.combining(c)).strip()

def tokens(text):
    return re.findall(r"[a-z0-9]+", fold(text))

def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None

def property_text(row):
    attrs = row.get("attributes") or {}
    return " ".join(str(v) for v in (
        row.get("property_type"), row.get("commune"), row.get("title"),
        row.get("description"), row.get("search_text"),
        " ".join(f"{k} {attrs[k]}" for k in ATTRIBUTES if k in attrs)
    ) if v)

def profile_text(profile):
    return " ".join(str(v) for v in (
        profile.get("query"), " ".join(profile.get("comunas") or []),
        " ".join(profile.get("property_types") or [])
    ) if v)

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

def filter_property(profile, row, now):
    """Hard mismatches are excluded; unknown evidence stays in review only."""
    if row.get("human_veto") or row.get("classification_state") in BROKERS:
        return "excluded", ["COMMERCIAL_VETO"]
    if row.get("published") is not True or row.get("operational_state") in {"REMOVED", "NOT_AVAILABLE"}:
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
    price = number(row.get("price_clp"))
    if not price:
        missing.append("PRICE_UNKNOWN")
    elif price > profile["max_price_clp"]:
        return "excluded", ["OVER_BUDGET"]
    if row.get("price_valid") is not True:
        missing.append("PRICE_UNVERIFIED")
    for key, minimum in (profile.get("minimum_attributes") or {}).items():
        actual = number((row.get("attributes") or {}).get(key))
        if actual is None:
            missing.append("UNKNOWN_" + key.upper())
        elif actual < minimum:
            return "excluded", ["MINIMUM_" + key.upper()]
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
        query_vector = self.embedder.encode([profile_text(profile)])[0] if self.vectors is not None else None
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
                "url": row.get("canonical_url"), "price_clp": row.get("price_clp"),
                "attributes": row.get("attributes") or {}, "commune": row.get("commune"),
                "score": score, "bm25": lexical, "cosine": dense, "review_reasons": reasons})
        for group in groups.values():
            group.sort(key=lambda x: (-x["score"], x["listing_key"]))
            if self.reranker and group:
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
                "reranked": self.reranker is not None,
                "scanned": len(self.rows), "excluded": dict(rejected),
                "compatible_count": len(groups["compatible"]), "review_count": len(groups["review"]),
                "compatible": groups["compatible"][:limit], "review": groups["review"][:limit]}
