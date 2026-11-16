"""Evita estados firmados sin evidencia y fallo de pypdf >= 7 en convenios."""
from io import BytesIO
from pathlib import Path

import pypdf

from services.pdf_generator_contracts import PDFGenerator
from services.security_contracts import SecurityContracts


def test_convenio_pdf_firmado_compatible_with_pypdf_7():
    original = PDFGenerator.generate_original_contract({
        "contract_code": "PROC-TEST-4578",
        "property_code": "17252",
        "cliente_nombre": "Cliente de prueba",
        "cliente_rut": "12345678-5",
        "tipo": "Arriendo",
        "propiedad_direccion": "Calle Prueba",
        "comuna": "Santiago",
        "rol": "123-4",
        "email": "test@example.org",
    })
    acceptance = {
        "contract_code": "PROC-TEST-4578",
        "transaction_uuid": "e2fbd9a0-3d36-4f5b-8f5b-98552b70d0ea",
        "server_timestamp": "2026-11-16T18:29:33+00:00",
        "user_agent": "Test suite",
        "ip": "127.0.0.1",
        "original_hash": SecurityContracts.hash_document(original),
        "timeline_hash": "b" * 64,
        "read_time_seconds": 30,
        "scrolled_to_bottom": "Sí",
    }
    signed = PDFGenerator.generate_signed_contract(
        original,
        {"client_data": {"nombre": "Cliente de prueba", "rut": "12345678-5"}},
        acceptance,
        "https://example.org/contracts/verify/PROC-TEST-4578",
    )
    doc = pypdf.PdfReader(BytesIO(signed))
    assert len(doc.pages) >= 2
    text = doc.pages[-1].extract_text()
    assert "CERTIFICADO DE FIRMA" in text
    assert acceptance["original_hash"] in text
    assert "Página" in text
    assert signed.startswith(b"%PDF")
    assert SecurityContracts.hash_document(signed) != SecurityContracts.hash_document(original)


def test_convenio_estado_signed_exige_hashes_y_archivo():
    from api_contracts import _signature_evidence_complete

    doc = {"status": "signed", "security": {
        "original_hash": "a" * 64,
        "signed_hash": "b" * 64,
        "timeline_hash": "c" * 64,
        "server_hmac": "d" * 64,
        "signed_pdf_drive_id": "drive-id",
    }}
    assert _signature_evidence_complete(doc)
    doc["security"]["server_hmac"] = None
    assert not _signature_evidence_complete(doc)
    doc["security"]["server_hmac"] = "d" * 64
    doc["security"]["signed_pdf_drive_id"] = None
    assert not _signature_evidence_complete(doc)
    doc["security"]["signed_pdf_drive_id"] = "drive-id"
    doc["status"] = "signature_processing_failed"
    assert not _signature_evidence_complete(doc)


def test_convenio_verificacion_publica_no_valida_solo_por_status():
    html = (Path(__file__).parents[1] / "templates" / "contract_verify.html").read_text("utf-8")
    assert "{% if verification_valid %}" in html
    assert "Integridad documental no confirmada" in html
    assert 'contract.status in ["accepted", "signed"]' not in html
