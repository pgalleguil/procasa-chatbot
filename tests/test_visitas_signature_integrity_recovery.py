"""Regression tests: an OTP acceptance is not a completed signed PDF."""
from io import BytesIO

import pypdf

from services.pdf_generator_visitas import PDFGeneratorVisitas
from services.security_contracts import SecurityContracts


def test_signed_pdf_generated_with_pypdf_modern_writer():
    original = PDFGeneratorVisitas.generate_original_contract({
        "visita_code": "VIS-TEST-1234",
        "property_code": "17252",
        "cliente_nombre": "Cliente de prueba",
        "cliente_rut": "12345678-5",
        "property_tipo": "Departamento",
        "property_comuna": "Santiago",
        "property_region": "Metropolitana",
        "ejecutivo_nombre": "Ejecutivo de prueba",
    })
    evidence = {
        "visita_code": "89b0327e-e640-4acc-8064-e0f5293d1fe7",
        "contract_code": "VIS-TEST-1234",
        "server_timestamp": "2026-11-16T18:29:33.733057+00:00",
        "ip": "127.0.0.1",
        "user_agent": "Test suite",
        "original_hash": SecurityContracts.hash_document(original),
        "timeline_hash": "b" * 64,
        "scrolled_to_bottom": "Sí",
        "read_time_seconds": 42,
    }
    result = PDFGeneratorVisitas.generate_signed_contract(
        original,
        {"client_data": {"nombre": "Cliente de prueba", "rut": "12345678-5"}},
        evidence,
        "https://example.org/visitas/verify/VIS-TEST-1234",
    )
    pdf = pypdf.PdfReader(BytesIO(result))
    assert len(pdf.pages) >= 2
    last_page = pdf.pages[-1].extract_text()
    assert "CERTIFICADO DE FIRMA" in last_page
    assert "18:29:33" not in last_page or "15:29:33" in last_page
    assert evidence["original_hash"] in last_page
    assert "Página" in last_page
    assert SecurityContracts.hash_document(result) != SecurityContracts.hash_document(original)


def test_signature_requires_complete_persisted_evidence():
    from api_visitas import _signature_evidence_complete

    signature = {
        "status": "signed",
        "security": {
            "token_used": True,
            "original_hash": "a" * 64,
            "signed_hash": "b" * 64,
            "timeline_hash": "c" * 64,
            "server_hmac": "d" * 64,
            "signed_pdf_drive_id": "drive-file-id",
        },
    }
    assert _signature_evidence_complete(signature)
    signature["security"]["signed_hash"] = None
    assert not _signature_evidence_complete(signature)
    signature["security"]["signed_hash"] = "b" * 64
    signature["security"]["server_hmac"] = None
    assert not _signature_evidence_complete(signature)
    signature["security"]["server_hmac"] = "d" * 64
    signature["security"]["signed_pdf_drive_id"] = None
    assert not _signature_evidence_complete(signature)


def test_verification_template_is_fail_closed():
    from pathlib import Path

    page = (Path(__file__).parents[1] / "templates" / "visita_verify.html").read_text("utf-8")
    assert "{% if verification_valid %}" in page
    assert "Integridad documental no confirmada" in page
    assert "contract.status in" not in page
