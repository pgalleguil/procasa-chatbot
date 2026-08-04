import os
import io
import json
import re
import logging
import unicodedata
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger("procasa-gdrive")


def sanitize_folder_name(raw: str, default: str = "Sin_Nombre") -> str:
    """Normaliza un nombre para usarlo como carpeta en Drive (sin acentos, espacios -> _)."""
    if not raw:
        return default
    raw = unicodedata.normalize('NFKD', raw)
    raw = ''.join(c for c in raw if not unicodedata.combining(c))
    raw = re.sub(r'[^A-Za-z0-9]+', '_', raw)
    raw = re.sub(r'_+', '_', raw).strip('_')
    return raw or default


def expedition_folder_name(client_name: str, property_code: str, default: str = "Expediente") -> str:
    """Nombre de carpeta por expediente: cliente + código de propiedad."""
    return f"Expediente_{sanitize_folder_name(client_name, 'Cliente')}_{sanitize_folder_name(property_code, 'Propiedad')}"

class GDriveSync:
    SCOPES = ['https://www.googleapis.com/auth/drive']
    
    def __init__(self, credentials_path: str = 'credentials.json', parent_folder_id: str = None):
        self.credentials_path = credentials_path
        self.parent_folder_id = parent_folder_id
        self.service = self._authenticate()

    def _authenticate(self):
        # Prioridad 1: credenciales en env var GDRIVE_CREDENTIALS_JSON (Render, sin subir el .json al repo)
        env_json = os.getenv("GDRIVE_CREDENTIALS_JSON")
        if env_json:
            try:
                creds = service_account.Credentials.from_service_account_info(
                    json.loads(env_json), scopes=self.SCOPES)
                return build('drive', 'v3', credentials=creds)
            except Exception as e:
                logger.error(f"Error autenticando desde GDRIVE_CREDENTIALS_JSON: {e}")
                return None
        # Prioridad 2: archivo local (solo desarrollo)
        if not os.path.exists(self.credentials_path):
            logger.warning(f"No se encontró GDRIVE_CREDENTIALS_JSON ni {self.credentials_path}. GDrive sync estará deshabilitado.")
            return None
        try:
            creds = service_account.Credentials.from_service_account_file(
                self.credentials_path, scopes=self.SCOPES)
            return build('drive', 'v3', credentials=creds)
        except Exception as e:
            logger.error(f"Error autenticando con Google Drive: {e}")
            return None

    def share_item(self, file_or_folder_id: str):
        """Otorga permiso para que el archivo/carpeta sea visible en Google Drive."""
        if not self.service or not file_or_folder_id or file_or_folder_id in ["mock_folder_id", "mock_file_id"]:
            return
        try:
            self.service.permissions().create(
                fileId=file_or_folder_id,
                body={'type': 'anyone', 'role': 'writer'},
                supportsAllDrives=True
            ).execute()
            logger.info(f"[GDRIVE] Permiso otorgado correctamente a {file_or_folder_id}")
        except Exception as e:
            logger.warning(f"[GDRIVE] No se pudo otorgar permiso compartibilidad a {file_or_folder_id}: {e}")

    def create_folder(self, folder_name: str) -> str:
        """Crea una carpeta y retorna su ID."""
        if not self.service:
            return "mock_folder_id"
            
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        if self.parent_folder_id:
            file_metadata['parents'] = [self.parent_folder_id]
            
        try:
            folder = self.service.files().create(
                body=file_metadata,
                fields='id',
                supportsAllDrives=True
            ).execute()
            folder_id = folder.get('id')
            if folder_id:
                self.share_item(folder_id)
            return folder_id
        except Exception as e:
            logger.error(f"Error creando carpeta en GDrive: {e}")
            return "mock_folder_id"

    def upload_file(self, folder_id: str, file_name: str, file_bytes: bytes, mime_type: str = 'application/pdf') -> str:
        """Sube un archivo (desde bytes) a la carpeta especificada."""
        if not self.service:
            logger.info(f"[MOCK GDRIVE] Subido {file_name} a carpeta {folder_id}")
            return "mock_file_id"
            
        try:
            file_metadata = {
                'name': file_name,
                'parents': [folder_id]
            }
            media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=True)
            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id',
                supportsAllDrives=True
            ).execute()
            file_id = file.get('id')
            if file_id:
                self.share_item(file_id)
            return file_id
        except Exception as e:
            logger.error(f"Error subiendo {file_name} a GDrive: {e}")
            return "mock_file_id"

    def download_file(self, file_id: str) -> bytes:
        """Descarga un archivo desde GDrive por su ID y retorna los bytes."""
        if not self.service or file_id == "mock_file_id":
            logger.info(f"[MOCK GDRIVE] Simulación de descarga fallida para {file_id}")
            return None
            
        try:
            from googleapiclient.http import MediaIoBaseDownload
            request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
            file_stream = io.BytesIO()
            downloader = MediaIoBaseDownload(file_stream, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
            return file_stream.getvalue()
        except Exception as e:
            logger.error(f"Error descargando archivo {file_id} desde GDrive: {e}")
            return None
