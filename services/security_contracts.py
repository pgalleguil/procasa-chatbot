import hashlib
import hmac
import random
import string
import json
from datetime import datetime, timezone
import os

class SecurityContracts:
    @staticmethod
    def generate_otp(length: int = 6) -> str:
        """Genera un OTP numérico de longitud especificada."""
        return ''.join(random.choices(string.digits, k=length))

    @staticmethod
    def generate_server_timestamp() -> str:
        """Genera un timestamp UTC inmutable desde el servidor."""
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def hash_document(file_bytes: bytes) -> str:
        """Calcula el hash SHA-256 de un documento PDF (bytes)."""
        return hashlib.sha256(file_bytes).hexdigest()

    @staticmethod
    def hash_timeline(timeline: list) -> str:
        """Calcula el hash SHA-256 de un timeline completo (lista de dicts)."""
        # Se ordena las keys para garantizar determinismo
        timeline_str = json.dumps(timeline, sort_keys=True)
        return hashlib.sha256(timeline_str.encode('utf-8')).hexdigest()

    @staticmethod
    def generate_server_hmac(contract_code: str, doc_hash: str, timestamp: str, secret_key: str) -> str:
        """
        Firma lógica del servidor (HMAC SHA-256).
        Permite demostrar que el contrato fue procesado y sellado por este servidor específico.
        """
        message = f"{contract_code}|{doc_hash}|{timestamp}".encode('utf-8')
        return hmac.new(secret_key.encode('utf-8'), message, hashlib.sha256).hexdigest()
