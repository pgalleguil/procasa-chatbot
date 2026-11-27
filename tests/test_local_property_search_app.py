import json
import pytest
from scripts.local_property_search_app import manual_profile, parse_amount, safe_url, classify_result, display_features, result_rows, write_export

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
    assert classify_result({"review_reasons":["PRICE_STALE"]},False)["ajuste"]=="Precio por verificar"

def test_group_preserved():
    row={"listing_key":"x","review_reasons":["PRICE_UNKNOWN"],"url":"file:///a"}
    result=result_rows({"compatible":[],"review":[row]},{"x":{"title":"casa"}})
    assert result[0]["grupo"]=="review"
    assert result[0]["url"]==""

def test_result_facts_are_sanitized_and_missing_sector_is_explicit():
    row={"listing_key":"x","review_reasons":["EXTERNAL_HUMAN_VERIFICATION"],"url":"https://example.cl/x",
         "attributes":{"bedrooms":3},"features":{"has_patio":True},"price_clp":50_000_000}
    catalog={"x":{"title":"Casa +56 9 1234 5678","description":"Visitas ventas@example.cl",
                   "commune":"Maipú","price_clp":50_000_000}}
    result=result_rows({"compatible":[],"review":[row]},catalog)[0]
    assert result["title"]=="Casa"
    assert result["description"]=="Visitas"
    assert result["commune"]=="Maipú"
    assert result["display_sector"]=="Sector no informado"
    assert "Dormitorios: 3" in result["attributes_display"]
    assert result["features_display"]=="Patio: Sí"

def test_translation_preserves_only_catalogued_features():
    assert display_features({"piscina":True,"has_air_conditioning":False}) == "Piscina: Sí · Aire acondicionado: No"

def test_export_is_local_new_file_and_protects_inputs(tmp_path):
    target=tmp_path/"resultados.json"
    reports=[{"profile_id":"manual","results":[{"title":"Casa"}]}]
    assert write_export(target,reports)==target
    assert json.loads(target.read_text(encoding="utf-8"))==reports
    with pytest.raises(FileExistsError):
        write_export(target,reports)
    with pytest.raises(ValueError):
        write_export(tmp_path/"catalog.json",reports,[tmp_path/"catalog.json"])
