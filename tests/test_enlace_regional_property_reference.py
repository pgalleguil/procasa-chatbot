import pytest
from chatbot.property_lookup import extract_property_external_id


@pytest.mark.parametrize("path", [
    "/usados/vina-del-mar/departamento/102006303/1349765",
    "/valparaiso/usados/vina-del-mar/departamento/102006303/1349765/",
    "/metropolitana/usados/santiago/departamento/102006303/1349765",
])
def test_enlace_exact_international_code(path):
    assert extract_property_external_id("https://www.enlaceinmobiliario.cl" + path) == "102006303"


@pytest.mark.parametrize("path", [
    "/valparaiso/usados/vina-del-mar/departamento/6303/1349765/",
    "/valparaiso/usados/vina-del-mar/departamento/102006303/",
    "/valparaiso/usados/vina-del-mar/departamento/102006303/1349765/extra",
])
def test_enlace_rejects_unverified_path_shapes(path):
    assert extract_property_external_id("https://www.enlaceinmobiliario.cl" + path) is None
