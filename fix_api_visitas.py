import re

path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\api_visitas.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

replacements = {
    'router = APIRouter(prefix="/contracts", tags=["Contracts"])': 'router = APIRouter(prefix="/visitas", tags=["Visitas"])',
    'logger = logging.getLogger("procasa-contracts")': 'logger = logging.getLogger("procasa-visitas")',
    'from services.pdf_generator_contracts import PDFGenerator': 'from services.pdf_generator_visitas import PDFGeneratorVisitas as PDFGenerator',
    '_CONTRACTS_DB_EXECUTOR': '_VISITAS_DB_EXECUTOR',
    'contracts_db': 'visitas_db',
    '["contracts"]': '["visitas"]',
    'contracts_pdf': 'visitas_pdf',
    '"contracts"': '"visitas"',
    "contract_code": "visita_code",
    "contract_doc": "visita_doc",
    "contract_rut": "visita_rut",
    "/contracts/view/": "/visitas/view/",
    "contract_view.html": "visita_view.html",
    "PROC-": "VIS-",
    "Contrato_": "Orden_Visita_",
    "Contrato_Autorizacion_": "Orden_Visita_",
    "contract_": "visita_",
    "contracts_": "visitas_",
    "Contrato": "Orden de Visita",
    "contrato": "orden de visita",
    "Convenio": "Orden de Visita",
    "convenio": "orden de visita",
    "El emisor del convenio siempre es el usuario autenticado.": "El emisor de la orden de visita siempre es el usuario autenticado.",
    # Update Whatsapp Message
    'Este proceso utiliza firma electrónica conforme a la Ley 19.799.': 'Este proceso utiliza firma electrónica conforme a la Ley 19.799 para la orden de visita.',
    'acepta el contrato asociado a su propiedad.': 'acepta la orden de visita de la propiedad.',
}

for k, v in replacements.items():
    content = content.replace(k, v)

# Re-enable the correct module imports if any were broken
with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Done fixing api_visitas.py!")
