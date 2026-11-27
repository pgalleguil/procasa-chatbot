import pytest
from scripts.local_property_search_app import manual_profile, parse_amount, safe_url, classify_result, result_rows

def test_manual_clp_and_minimums():
    p=manual_profile("VENTA","Maipú, Cerrillos","CASA","110.000.000","patio",{"bedrooms":"3","parking":"1"})
    assert p["max_price_clp"]==110_000_000
    assert p["comunas"]==["Maipú","Cerrillos"]
    assert p["minimum_attributes"]=={"bedrooms":3,"parking":1}

@pytest.mark.parametrize("value",["1.2","-100","0","1,000","1e8",""])
def test_invalid_budget(value):
    with pytest.raises(ValueError):
        parse_amount(value)

def test_location_stays_pending():
    p=manual_profile("ARRIENDO","Maipú","CASA","500000","patio",{},"colegio")
    assert p["location_targets"]==["colegio"]
    assert "verified_location" not in p

def test_links_and_states():
    assert safe_url("javascript:alert(1)")==""
    assert safe_url("https://user:pass@example.com")==""
    assert safe_url("https://example.com/aviso")=="https://example.com/aviso"
    x=classify_result({"review_reasons":["EXTERNAL_HUMAN_VERIFICATION"]},False)
    assert x["ajuste"]=="Cumple filtros registrados"
    assert "no ofrecer" in x["estado"]
    assert classify_result({"review_reasons":["UNKNOWN_BEDROOMS"]},False)["ajuste"]=="Datos obligatorios pendientes"

def test_group_preserved():
    row={"listing_key":"x","review_reasons":["PRICE_UNKNOWN"],"url":"file:///a"}
    result=result_rows({"compatible":[],"review":[row]},{"x":{"title":"casa"}})
    assert result[0]["grupo"]=="review"
    assert result[0]["url"]==""
