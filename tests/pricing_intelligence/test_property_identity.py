from analytics.pricing_intelligence.models import LinkageStatus
from analytics.pricing_intelligence.property_identity import AliasKey, build_property_identity_resolver


def property_doc(code, *, mercadolibre=None, yapo=None):
    publications = {}
    if mercadolibre is not None:
        publications["portal_inmobiliario"] = {"publicaciones": {"V": {"code": mercadolibre}}}
    if yapo is not None:
        publications["yapo"] = {"publicaciones": {"V": {"code": yapo}}}
    return {"codigo": code, "publicaciones": publications}


def lead(*, canonical=None, mercadolibre=None, yapo=None):
    prospecto = {}
    if canonical is not None:
        prospecto["codigo"] = canonical
    if mercadolibre is not None:
        prospecto["codigo_mercadolibre"] = mercadolibre
    if yapo is not None:
        prospecto["codigo_yapo"] = yapo
    return {"prospecto": prospecto}


def test_canonical_exact():
    resolver = build_property_identity_resolver([property_doc("0007")])
    result = resolver.resolve(lead(canonical="0007"))
    assert result.status is LinkageStatus.EXACT_CANONICAL
    assert result.resolved_property_code == "0007"


def test_alias_exact():
    resolver = build_property_identity_resolver([property_doc("P1", yapo="000123")])
    result = resolver.resolve(lead(yapo="000123"))
    assert result.status is LinkageStatus.EXACT_ALIAS
    assert result.resolved_property_code == "P1"


def test_canonical_and_alias_same_property_is_canonical():
    resolver = build_property_identity_resolver([property_doc("P1", yapo="Y1")])
    result = resolver.resolve(lead(canonical="P1", yapo="Y1"))
    assert result.status is LinkageStatus.EXACT_CANONICAL
    assert set(result.candidates) == {"P1"}


def test_canonical_a_and_alias_b_is_conflict():
    resolver = build_property_identity_resolver(
        [property_doc("A"), property_doc("B", yapo="Y-B")]
    )
    result = resolver.resolve(lead(canonical="A", yapo="Y-B"))
    assert result.status is LinkageStatus.CONFLICT
    assert result.resolved_property_code is None
    assert set(result.candidates) == {"A", "B"}


def test_alias_associated_to_two_properties_is_ambiguous():
    resolver = build_property_identity_resolver(
        [property_doc("A", yapo="SAME"), property_doc("B", yapo="SAME")]
    )
    result = resolver.resolve(lead(yapo="SAME"))
    assert result.status is LinkageStatus.AMBIGUOUS
    assert result.resolved_property_code is None
    assert set(result.candidates) == {"A", "B"}


def test_identifier_not_found_is_unmatched():
    resolver = build_property_identity_resolver([property_doc("A")])
    result = resolver.resolve(lead(canonical="MISSING", yapo="MISSING"))
    assert result.status is LinkageStatus.UNMATCHED


def test_leading_zeroes_are_preserved():
    resolver = build_property_identity_resolver([property_doc("000123", yapo="000456")])
    assert resolver.resolve(lead(canonical="123")).status is LinkageStatus.UNMATCHED
    assert resolver.resolve(lead(canonical="000123")).status is LinkageStatus.EXACT_CANONICAL
    assert resolver.resolve(lead(yapo="000456")).resolved_property_code == "000123"


def test_source_namespace_prevents_cross_portal_collision():
    resolver = build_property_identity_resolver(
        [property_doc("Y", yapo="456789"), property_doc("M", mercadolibre="456789")]
    )
    assert resolver.resolve(lead(yapo="456789")).resolved_property_code == "Y"
    assert resolver.resolve(lead(mercadolibre="456789")).resolved_property_code == "M"
    assert AliasKey("yapo", "456789") != AliasKey("mercadolibre", "456789")
