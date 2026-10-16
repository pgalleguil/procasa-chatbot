import unittest
from unittest.mock import MagicMock, patch
from chatbot.storage import get_user_by_phone

class TestExecutiveFilter(unittest.TestCase):

    @patch("chatbot.storage.get_db")
    def test_get_user_by_phone(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        
        # Mocking find_one to return a user
        mock_db["usuarios"].find_one.return_value = {
            "nombre": "Susana Ensignia",
            "rol": "agente",
            "telefono": "+56939125978"
        }
        
        user = get_user_by_phone("+56 9 3912 5978")
        self.assertIsNotNone(user)
        self.assertEqual(user["nombre"], "Susana Ensignia")
        
        # Verify normalization
        mock_db["usuarios"].find_one.assert_called_with({
            "telefono": {"$regex": "56939125978"}
        })

    def test_executive_filter_logic(self):
        # Simulation of the logic added to webhook.py
        def should_ignore_and_set_from_me(user_found):
            from_me = False
            if user_found and user_found.get("rol") in ["agente", "supervisor"]:
                if user_found.get("nombre") != "Control Owner":
                    from_me = True
            return from_me

        # Case 1: Executive (Susana) -> Should set from_me = True
        user_susana = {"nombre": "Susana Ensignia", "rol": "agente"}
        self.assertTrue(should_ignore_and_set_from_me(user_susana))

        # Case 2: Control Owner -> Should set from_me = False
        user_pablo = {"nombre": "Control Owner", "rol": "agente"}
        self.assertFalse(should_ignore_and_set_from_me(user_pablo))

        # Case 3: Customer (Not in usuarios) -> Should set from_me = False
        self.assertFalse(should_ignore_and_set_from_me(None))

        # Case 4: Supervisor (Not Pablo) -> Should set from_me = True
        user_supervisor = {"nombre": "Jorge Caro", "rol": "supervisor"}
        self.assertTrue(should_ignore_and_set_from_me(user_supervisor))

if __name__ == "__main__":
    unittest.main()
