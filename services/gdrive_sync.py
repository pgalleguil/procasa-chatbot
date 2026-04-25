import os
import io
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger("procasa-gdrive")

class GDriveSync:
    SCOPES = ['https://www.googleapis.com/auth/drive.file']
    
    def __init__(self, credentials_path: str = 'credentials.json', parent_folder_id: str = None):
        self.credentials_path = credentials_path
        self.parent_folder_id = parent_folder_id
        self.service = self._authenticate()

    def _authenticate(self):
        if not os.path.exists(self.credentials_path):
            logger.warning(f"No se encontró {self.credentials_path}. GDrive sync estará deshabilitado.")
            return None
        try:
            creds = service_account.Credentials.from_service_account_file(
                self.credentials_path, scopes=self.SCOPES)
            return build('drive', 'v3', credentials=creds)
        except Exception as e:
            logger.error(f"Error autenticando con Google Drive: {e}")
            return None

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
            folder = self.service.files().create(body=file_metadata, fields='id').execute()
            return folder.get('id')
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
            file = self.service.files().create(body=file_metadata, media_body=media, fields='id').execute()
            return file.get('id')
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
            request = self.service.files().get_media(fileId=file_id)
            file_stream = io.BytesIO()
            downloader = MediaIoBaseDownload(file_stream, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
            return file_stream.getvalue()
        except Exception as e:
            logger.error(f"Error descargando archivo {file_id} desde GDrive: {e}")
            return None
