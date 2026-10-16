import unittest
from unittest.mock import MagicMock, patch
from config import Config
import re


def _matches_query(doc, query):
    for field, expected in query.items():
        value = doc
        for part in field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(expected, dict) and "$regex" in expected:
            if not re.search(expected["$regex"], str(value or ""), re.I):
                return None
        elif isinstance(expected, dict) and "$in" in expected:
            if value not in expected["$in"]:
                return None
        elif value != expected:
            return None
    return doc


class TestPropertyResolution(unittest.TestCase):
    @patch("chatbot.core.get_db")
    def test_url_is_resolved_against_universo_cartera(self, mock_get_db):
        from chatbot.core import _buscar_propiedad_en_universo

        mock_db = MagicMock()
        mock_col = MagicMock()
        mock_db.__getitem__.side_effect = lambda name: mock_col if name in {"universo_cartera", Config.PROPERTY_COLLECTION_NAME} else MagicMock()
        mock_get_db.return_value = mock_db

        doc = {
            "codigo": "12345", "comuna": "Santiago",
            "publicaciones": {"yapo": {"url_yapo": "https://www.yapo.cl/chile/propiedad/12345"}},
        }
        mock_col.find_one.side_effect = lambda query: _matches_query(doc, query)

        prop = _buscar_propiedad_en_universo(mock_db, "https://www.yapo.cl/chile/propiedad/12345")

        self.assertIsNotNone(prop)
        self.assertEqual(prop["codigo"], "12345")
        self.assertEqual(prop["comuna"], "Santiago")

        # Verificamos que la búsqueda contempla URL y códigos comunes de la colección
        called_queries = [call.args[0] for call in mock_col.find_one.call_args_list]
        self.assertTrue(
            any("publicaciones.yapo.url_yapo" in query for query in called_queries if isinstance(query, dict))
        )

    @patch("chatbot.core.get_db")
    def test_portal_specific_search_is_prioritized(self, mock_get_db):
        from chatbot.core import _buscar_propiedad_en_universo

        mock_db = MagicMock()
        mock_col = MagicMock()
        mock_db.__getitem__.side_effect = lambda name: mock_col if name in {"universo_cartera", Config.PROPERTY_COLLECTION_NAME} else MagicMock()
        mock_get_db.return_value = mock_db

        doc = {
            "codigo": "77777", "comuna": "Providencia",
            "publicaciones": {"portal_inmobiliario": {"url_pi": "https://www.portalinmobiliario.com/MLC-77777"}},
        }
        mock_col.find_one.side_effect = lambda query: _matches_query(doc, query)

        prop = _buscar_propiedad_en_universo(
            mock_db,
            "https://www.portalinmobiliario.com/MLC-77777",
            portal="PortalInmobiliario"
        )

        self.assertIsNotNone(prop)
        self.assertEqual(prop["codigo"], "77777")
        called_queries = [call.args[0] for call in mock_col.find_one.call_args_list]
        self.assertTrue(
            any("publicaciones.portal_inmobiliario.url_pi" in query for query in called_queries if isinstance(query, dict))
        )


if __name__ == "__main__":
    unittest.main()
