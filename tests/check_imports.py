
import sys
import os

# Add parent dir to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from chatbot import alert_service
    print("Successfully imported chatbot.alert_service")
except Exception as e:
    print(f"Failed to import chatbot.alert_service: {e}")
    sys.exit(1)
