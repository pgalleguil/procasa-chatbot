
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

# Add parent dir to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from chatbot.lead_router import find_responsible_executive, should_send_now
from datetime import datetime, time

class TestLeadRouter(unittest.TestCase):
    
    @patch('chatbot.lead_router.get_db')
    def test_jpc_nunoa(self, mock_get_db):
        # Setup mock property
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db["universo_obelix"].find_one.return_value = {
            "codigo": "123",
            "ejecutivo": "Jorge Pablo Caro",
            "region": "XIII Región Metropolitana",
            "comuna": "Ñuñoa"
        }
        
        # Test
        name, phone = find_responsible_executive("123")
        self.assertEqual(name, "Mariela Arriagada")

    @patch('chatbot.lead_router.get_db')
    def test_jpc_providencia(self, mock_get_db):
        # Setup mock property
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db["universo_obelix"].find_one.return_value = {
            "codigo": "124",
            "ejecutivo": "Jorge Pablo Caro",
            "region": "XIII Región Metropolitana",
            "comuna": "Providencia"
        }
        
        # Test
        name, phone = find_responsible_executive("124")
        self.assertEqual(name, "Mariela Arriagada")

    @patch('chatbot.lead_router.get_db')
    def test_jpc_valpo(self, mock_get_db):
        # Setup mock property
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db["universo_obelix"].find_one.return_value = {
            "codigo": "125",
            "ejecutivo": "Jorge Pablo Caro",
            "region": "V Región de Valparaíso",
            "comuna": "Viña del Mar"
        }
        
        # Test
        name, phone = find_responsible_executive("125")
        self.assertEqual(name, "Erika Garrido")

    @patch('chatbot.lead_router.get_db')
    def test_jpc_other_commune_rm(self, mock_get_db):
        # Setup mock property
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db["universo_obelix"].find_one.return_value = {
            "codigo": "126",
            "ejecutivo": "Jorge Pablo Caro",
            "region": "XIII Región Metropolitana",
            "comuna": "La Florida"
        }
        
        # Test - Should be Susana or Erika
        name, phone = find_responsible_executive("126")
        self.assertIn(name, ["Susana Ensignia", "Erika Garrido"])

    @patch('chatbot.lead_router.get_db')
    def test_other_executive(self, mock_get_db):
        # Setup mock property
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db["universo_obelix"].find_one.return_value = {
            "codigo": "127",
            "ejecutivo": "Pedro Perez",
            "region": "XIII Región Metropolitana",
            "comuna": "Santiago"
        }
        
        # Test
        name, phone = find_responsible_executive("127")
        self.assertEqual(name, "Pedro Perez")

    @patch('chatbot.lead_router.datetime')
    def test_time_logic_weekend(self, mock_datetime):
        # Mock Sunday (weekday = 6)
        mock_now = MagicMock()
        mock_now.weekday.return_value = 6
        mock_datetime.now.return_value = mock_now
        
        self.assertFalse(should_send_now())

    @patch('chatbot.lead_router.datetime')
    def test_time_logic_after_hours(self, mock_datetime):
        # Mock Monday (weekday = 0) at 19:00
        mock_now = MagicMock()
        mock_now.weekday.return_value = 0
        mock_now.time.return_value = time(19, 0)
        mock_datetime.now.return_value = mock_now
        
        self.assertFalse(should_send_now())

    @patch('chatbot.lead_router.datetime')
    def test_time_logic_business_hours(self, mock_datetime):
        # Mock Monday (weekday = 0) at 10:00
        mock_now = MagicMock()
        mock_now.weekday.return_value = 0
        mock_now.time.return_value = time(10, 0)
        mock_datetime.now.return_value = mock_now
        
        self.assertTrue(should_send_now())

if __name__ == '__main__':
    unittest.main()
