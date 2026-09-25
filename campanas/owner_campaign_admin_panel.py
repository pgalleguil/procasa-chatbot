"""Temporary, CRM-admin-only control panel for fixed-recipient E2E previews."""

from __future__ import annotations

import asyncio
import html
import os
import re
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from campanas.owner_campaign_test_actions import TEST_CAMPAIGN_ID, TEST_RECIPIENT, test_mode_enabled
from analytics.owner_campaign_test_sender import ALL_CASES, TEST_RUN_ID, mass_send_enabled
from config import Config


TOKEN_SECRET_ENV = "OWNER_CAMPAIGN_TEST_TOKEN_SECRET"
CONFIRMATION_VALUE = "RUN_OWNER_CAMPAIGN_TEST"
DELIVERY_UNKNOWN_BASELINE = 6
EXPECTED_B_BUILT_SOURCE = "tasaciones.tasacion_online.total_construccion_m2:SII"
_SALE_TERMS = (
    "compradores", "comprador", "tasación", "precio de cierre",
    "precio final de venta", "subsidio", "publicación de venta",
)


def _secret_present() -> bool:
    return bool(os.environ.get(TOKEN_SECRET_ENV, "").strip())


def _mongo_db_and_ping() -> Any:
    from chatbot.storage import get_db

    database = get_db()
    database.client.admin.command("ping")
    return database


def _test_run_registered(database: Any) -> bool:
    return database["ajuste_precio"].find_one({"campaign_id": TEST_CAMPAIGN_ID}) is not None


def _disable_preview_actions(document: str) -> str:
    # Keep the approved rendered email intact except for hrefs: clicking in a
    # preview must not record a test action or open a report before the email.
    return re.sub(r"(?is)\shref\s*=\s*([\"']).*?\1", ' href="#preview-only"', document)


def _property_cases(case: Any) -> tuple[Any, ...]:
    return tuple(case.portfolio_cases) if case.case_id == "E" else (case,)


def _case_checks(preview: Any) -> tuple[dict[str, Any], str]:
    case_id = preview.case_id
    item = preview.prepared
    if item is None:
        return {
            "render_ok": False,
            "signed_url_valid": False,
            "no_localhost_url": False,
            "recipient_locked": False,
            "case_contract": False,
        }, f"No se pudo preparar el preview con datos actuales. {preview.case_id}_ERROR_CODE={html.escape(preview.error_code or 'preview_build_failed')}"

    case = item.case
    cases = _property_cases(case)
    common = {
        "render_ok": bool(item.html.strip() and item.text.strip()),
        # prepare_test_messages verifies each token signature, action, code,
        # host, document type, expiry and the fixed recipient.
        "signed_url_valid": True,
        "no_localhost_url": "localhost" not in item.html.casefold() and "127.0.0.1" not in item.html,
        "recipient_locked": TEST_RECIPIENT == "jpcaro@procasa.cl",
    }
    passed = False
    detail = ""
    if case_id == "A":
        model = case.render_context.get("property_model") or {}
        passed = (
            case.property_code == "5641"
            and case.evidence_segment == "PRICE_AUTHORIZATION_READY"
            and case.cta_type == "PRICE_AUTHORIZATION"
            and "aceptar nuevo valor" in str((model.get("cta") or {}).get("primary_label") or "").casefold()
        )
        detail = "5641 · autorización de nuevo valor" if passed else "A no cumple el contrato de autorización aprobado."
    elif case_id == "B":
        source = case.render_context.get("source_checks") or {}
        passed = (
            case.property_code == "16521"
            and case.evidence_segment == "MIXED_EVIDENCE"
            and case.cta_type == "ADVISOR_REVIEW"
            and source.get("property_built_m2") is not None
            and abs(float(source["property_built_m2"]) - 64.0) < 0.001
            and source.get("property_built_m2_source") == EXPECTED_B_BUILT_SOURCE
            and source.get("own_listing_excluded") is True
        )
        detail = (
            f"16521 · {source.get('property_built_m2')} m² · {source.get('property_built_m2_source')}"
            if passed else "B no confirma superficie SII, exclusión propia o CTA de asesor."
        )
    elif case_id == "C":
        model = case.render_context.get("property_model") or {}
        comparable = model.get("comparable") or {}
        top_refs = comparable.get("top3") or []
        land_refs = comparable.get("land_top3") or []
        top_ids = {str(ref.get("listing_id") or "") for ref in top_refs}
        land_ids = {str(ref.get("listing_id") or "") for ref in land_refs}
        source = case.render_context.get("source_checks") or {}
        passed = (
            case.property_code == "16486"
            and comparable.get("effective_type") == "PARCEL_WITH_IMPROVEMENTS"
            and source.get("primary_surface") == "built_m2"
            and source.get("integral_comparables_n", 0) >= 3
            and source.get("land_references_n", 0) >= 1
            and len(top_ids) == len(top_refs) and all(top_ids)
            and len(land_ids) == len(land_refs) and all(land_ids)
            and not (top_ids & land_ids)
        )
        detail = "16486 · referencias de suelo separadas de comparables integrales" if passed else "C mezcla o no separa referencias de suelo."
    elif case_id == "D":
        sale_fields = any(term in item.text.casefold() for term in _SALE_TERMS)
        passed = case.property_code == "16527" and case.operation == "ARRIENDO" and not sale_fields
        detail = "16527 · ARRIENDO · sin campos de venta" if passed else "D contiene operación o campos de venta incorrectos."
    elif case_id == "E":
        action_links = len(re.findall(r"/campana/test-accion\?token=", item.html))
        report_links = len(re.findall(r"/campana/informe\?token=", item.html))
        passed = len(cases) >= 3 and action_links == len(cases) and report_links == len(cases)
        detail = f"Cartera · {len(cases)} propiedades · token y CTA por propiedad" if passed else "E no tiene al menos 3 propiedades con links individuales."
    common["case_contract"] = passed
    return common, detail


def _all_preview_checks(previews: tuple[Any, ...]) -> tuple[bool, dict[str, tuple[dict[str, Any], str]]]:
    by_case = {preview.case_id: preview for preview in previews}
    results: dict[str, tuple[dict[str, Any], str]] = {}
    for case_id in ALL_CASES:
        preview = by_case.get(case_id)
        if preview is None:
            results[case_id] = ({"render_ok": False, "signed_url_valid": False,
                                 "no_localhost_url": False, "recipient_locked": False,
                                 "case_contract": False}, "Preview no generado.")
        else:
            results[case_id] = _case_checks(preview)
    passed = all(all(checks.values()) for checks, _detail in results.values())
    return passed, results


def _render_panel(*, commit: str, secret_present: bool, mongo_ready: bool,
                  smtp_ready: bool, test_mode: bool, mass_send: bool,
                  delivery_unknown: int | None, previews: tuple[Any, ...] | None,
                  qa_evidence: Mapping[str, Any] | None = None,
                  preview_error: bool = False, send_result: Mapping[str, Any] | None = None,
                  run_already_registered: bool = False) -> HTMLResponse:
    checks: dict[str, tuple[dict[str, Any], str]] = {}
    if previews is not None:
        all_cases_pass, checks = _all_preview_checks(previews)
    else:
        all_cases_pass = False
    runtime_ready = secret_present and mongo_ready and smtp_ready and test_mode and not mass_send
    delivery_ready = delivery_unknown == DELIVERY_UNKNOWN_BASELINE
    can_send = bool(
        previews is not None and all_cases_pass and runtime_ready and delivery_ready
        and not run_already_registered and send_result is None
    )
    case_map = {preview.case_id: preview for preview in previews or ()}

    case_rows = []
    preview_sections = []
    for case_id in ALL_CASES:
        preview = case_map.get(case_id)
        result = checks.get(case_id, ({}, "Preview bloqueado."))
        case_checks, detail = result
        item = preview.prepared if preview else None
        case = item.case if item else None
        code = case.property_code if case else ({"A": "5641", "B": "16521", "C": "16486", "D": "16527", "E": "propietario vigente ≥3"}[case_id])
        passed = bool(case_checks) and all(case_checks.values())
        state = "PASS" if passed else "REVISAR / BLOQUEADO"
        if case and case_id == "E":
            case_facts = "<br>".join(
                f"{html.escape(str(part.property_code))} · {html.escape(part.operation)} · "
                f"{html.escape(part.evidence_segment)} · {html.escape(part.cta_type)}"
                for part in _property_cases(case)
            )
        elif case:
            case_facts = (
                f"{html.escape(case.operation)} · {html.escape(case.evidence_segment)} · "
                f"{html.escape(case.cta_type)}"
            )
        else:
            case_facts = "Datos actuales no disponibles"
        preview_link = f'<a href="#preview-{case_id}">Ver preview {case_id}</a>' if item else "Preview no disponible"
        flags = " · ".join(f"{html.escape(key)}={'PASS' if value else 'FAIL'}" for key, value in case_checks.items())
        case_rows.append(
            f"<tr><td>{case_id}</td><td>{html.escape(str(code))}</td><td>{case_facts}<br>{html.escape(detail)}</td>"
            f"<td>{state}</td><td>{flags}</td><td>{preview_link}</td></tr>"
        )
        if item:
            safe_preview = _disable_preview_actions(item.html)
            srcdoc = html.escape(safe_preview, quote=True)
            preview_sections.append(
                f'<details id="preview-{case_id}"><summary>Preview {case_id} · {html.escape(str(code))}</summary>'
                f'<p class="muted">Links de acción e informe desactivados en esta vista previa.</p>'
                f'<iframe title="Preview {case_id}" sandbox srcdoc="{srcdoc}"></iframe></details>'
            )
    all_pass_text = "PASS" if all_cases_pass else "FAIL"
    delivery_text = str(delivery_unknown) if delivery_unknown is not None else "NO DISPONIBLE"
    evidence = qa_evidence or {}
    status_block = ""
    if send_result is not None:
        sent = int(send_result.get("test_emails_sent") or 0)
        status_block = (
            f'<section class="result"><h2>Resultado de envío de prueba</h2>'
            f'<p>Estado: <strong>{html.escape(str(send_result.get("status") or "desconocido"))}</strong></p>'
            f'<p>Mensajes de prueba enviados: <strong>{sent}</strong> · destinatario: <strong>{TEST_RECIPIENT}</strong></p>'
            f'<p>Emails a propietarios: <strong>0</strong> · cambio de precio real: <strong>NO</strong></p>'
            f'<p>delivery_unknown antes: {html.escape(str(send_result.get("delivery_unknown_before")))} · '
            f'después: {html.escape(str(send_result.get("delivery_unknown_after")))} · '
            f'nuevos: {html.escape(str(send_result.get("new_delivery_unknown")))}</p></section>'
        )
    elif preview_error:
        status_block = '<section class="result"><p>La preparación de previews no terminó. No se habilitó SMTP.</p></section>'

    form = ""
    if can_send:
        form = f"""<section class="confirm"><h2>Envío de pruebas</h2>
<p>Se enviarán como máximo cinco mensajes, exclusivamente a <strong>{TEST_RECIPIENT}</strong>.
No se enviará a propietarios y no se modificará ningún precio real.</p>
<form method="post" action="/owner-campaign-email-test/send">
<label><input type="checkbox" name="confirmation" value="{CONFIRMATION_VALUE}" required>
Enviar pruebas exclusivamente a {TEST_RECIPIENT}</label>
<p><button type="submit">Confirmar y enviar A–E</button></p></form></section>"""
    else:
        reasons = []
        if not runtime_ready: reasons.append("modo de prueba, secreto, Mongo, SMTP o envío masivo no está en estado seguro")
        if not delivery_ready: reasons.append("delivery_unknown debe estar exactamente en 6")
        if run_already_registered: reasons.append("el lote fijo ya está registrado en el ledger y no se repetirá")
        if not all_cases_pass: reasons.append("uno o más preflight A–E no pasó")
        form = '<section class="confirm"><h2>Envío deshabilitado</h2><p>' + html.escape("; ".join(reasons) or "Previews no disponibles.") + "</p></section>"

    document = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>QA campaña propietarios · PROCASA</title>
<style>body{{font:15px Arial,sans-serif;background:#f3f5fb;color:#172554;margin:0}}main{{max-width:1180px;margin:24px auto;padding:0 16px}}
section{{background:#fff;border:1px solid #dce2ef;border-radius:12px;padding:18px;margin:14px 0}}h1,h2{{margin-top:0}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}}
.stat{{background:#f7f8fc;padding:12px;border-radius:8px}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:9px;border-bottom:1px solid #e5e7eb;vertical-align:top}}td{{font-size:13px}}iframe{{width:100%;height:920px;border:1px solid #d9deea;background:white}}details{{margin:12px 0}}summary{{cursor:pointer;font-weight:bold;padding:10px;background:#f7f8fc}}.muted{{color:#64748b}}.confirm{{border-color:#6d5ce7}}button{{background:#26177a;color:#fff;border:0;padding:12px 18px;border-radius:7px;font-weight:bold;cursor:pointer}}.result{{background:#eef2ff}}</style></head>
<body><main><section><h1>QA temporal · campaña de propietarios</h1>
<p>Herramienta temporal de QA de correo, independiente del CRM · los links dentro de los previews son inertes.</p>
<div class="grid"><div class="stat">Commit desplegado<br><strong>{html.escape(commit)}</strong></div>
<div class="stat">TEST_RUN_ID<br><strong>{TEST_RUN_ID}</strong></div>
<div class="stat">TOKEN_SECRET_PRESENT<br><strong>{str(secret_present).lower()}</strong></div>
<div class="stat">Mongo disponible<br><strong>{str(mongo_ready).upper()}</strong></div>
<div class="stat">SMTP disponible<br><strong>{str(smtp_ready).upper()}</strong></div>
<div class="stat">TEST_MODE / MASS_SEND<br><strong>{str(test_mode).upper()} / {str(mass_send).upper()}</strong></div>
<div class="stat">delivery_unknown<br><strong>{html.escape(delivery_text)}</strong> · esperado 6</div>
<div class="stat">QA_EVIDENCE_SOURCE<br><strong>{html.escape(str(evidence.get('source') or 'NO DISPONIBLE'))}</strong></div>
<div class="stat">QA_EVIDENCE_SHA256<br><strong>{html.escape(str(evidence.get('sha256') or 'NO DISPONIBLE'))}</strong></div>
<div class="stat">QA_EVIDENCE_RECORDS<br><strong>{html.escape(str(evidence.get('records') or 'NO DISPONIBLE'))}</strong></div>
<div class="stat">Preflight A–E<br><strong>{all_pass_text}</strong></div>
<div class="stat">Destinatario fijo<br><strong>{TEST_RECIPIENT}</strong></div></div>
<p>Campaña: <code>{TEST_CAMPAIGN_ID}</code> · casos fijos: A 5641, B 16521, C 16486, D 16527 y E cartera vigente de 3 o más propiedades.</p>
</section><section><h2>Estado y previews</h2><div style="overflow-x:auto"><table><thead><tr><th>Caso</th><th>Código</th><th>Operación · segmento · CTA · contrato</th><th>Estado</th><th>Validaciones</th><th>Preview</th></tr></thead><tbody>{''.join(case_rows)}</tbody></table></div></section>
{''.join(preview_sections)}{form}{status_block}
<section class="muted"><strong>Seguridad:</strong> GET nunca envía. El POST acepta sólo confirmación literal; no recibe destinatario, precio, código ni campaign_id. La identidad del propietario se conserva sólo para auditoría. No hay envío productivo.</section>
</main></body></html>"""
    return HTMLResponse(document, status_code=200, headers={
        "Cache-Control": "private, no-store",
        "X-Robots-Tag": "noindex, nofollow, noarchive",
    })


async def handle_panel_get(request: Any) -> HTMLResponse:
    if request.query_params:
        raise HTTPException(status_code=400, detail="No se admiten parámetros en la URL")
    if not test_mode_enabled():
        raise HTTPException(status_code=404, detail="Herramienta temporal no disponible")

    secret_present = _secret_present()
    smtp_ready = bool(Config.GMAIL_USER and Config.GMAIL_PASSWORD)
    mongo_ready = False
    run_already_registered = False
    delivery_unknown: int | None = None
    previews = None
    qa_evidence = None
    preview_error = False
    commit = os.environ.get("RENDER_GIT_COMMIT", "unknown")
    try:
        delivery_unknown = await asyncio.to_thread(_read_delivery_unknown)
    except Exception:
        delivery_unknown = None
    if secret_present:
        try:
            from analytics.owner_campaign_report_normalization import build_rendered_test_previews
            from campanas.owner_campaign_test_runtime import qa_evidence_metadata

            database = await asyncio.to_thread(_mongo_db_and_ping)
            mongo_ready = True
            run_already_registered = await asyncio.to_thread(_test_run_registered, database)
            qa_evidence = await asyncio.to_thread(qa_evidence_metadata)
            previews = await asyncio.to_thread(build_rendered_test_previews, database)
            preview_error = any(item.prepared is None for item in previews)
        except Exception:
            preview_error = True
    else:
        try:
            await asyncio.to_thread(_mongo_db_and_ping)
            mongo_ready = True
        except Exception:
            mongo_ready = False
    return _render_panel(
        commit=commit, secret_present=secret_present, mongo_ready=mongo_ready,
        smtp_ready=smtp_ready, test_mode=True, mass_send=mass_send_enabled(),
        delivery_unknown=delivery_unknown, previews=previews, qa_evidence=qa_evidence, preview_error=preview_error,
        run_already_registered=run_already_registered,
    )


def _read_delivery_unknown() -> int:
    from analytics.owner_campaign_test_sender import delivery_unknown_count

    return delivery_unknown_count()


def _validate_same_origin_post(request: Any) -> None:
    if request.query_params:
        raise HTTPException(status_code=400, detail="No se admiten parámetros en la URL")
    origin = str(request.headers.get("origin") or "")
    host = str(request.headers.get("host") or "").casefold()
    parsed = urlsplit(origin)
    if not host or not parsed.netloc or parsed.netloc.casefold() != host or parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=403, detail="Origen no permitido")
    if Config.IS_PRODUCTION and parsed.scheme != "https":
        raise HTTPException(status_code=403, detail="Origen no seguro")


async def handle_panel_run(request: Any) -> HTMLResponse:
    if not test_mode_enabled():
        raise HTTPException(status_code=404, detail="Herramienta temporal no disponible")
    _validate_same_origin_post(request)
    if str(request.headers.get("content-type") or "").split(";", 1)[0].strip().casefold() != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Formato de confirmación no permitido")
    body = await request.body()
    if len(body) > 1024:
        raise HTTPException(status_code=413, detail="Confirmación inválida")
    try:
        form = parse_qs(body.decode("ascii"), strict_parsing=True, keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Confirmación inválida") from None
    if set(form) != {"confirmation"} or form.get("confirmation") != [CONFIRMATION_VALUE]:
        raise HTTPException(status_code=400, detail="Se requiere confirmación literal")
    if not _secret_present() or not Config.GMAIL_USER or not Config.GMAIL_PASSWORD or mass_send_enabled():
        raise HTTPException(status_code=409, detail="Precondiciones de prueba no disponibles")

    try:
        from analytics.owner_campaign_report_normalization import build_rendered_test_previews
        from analytics.owner_campaign_test_sender import TestCampaignCLIError, run_test_batch

        database = await asyncio.to_thread(_mongo_db_and_ping)
        if await asyncio.to_thread(_test_run_registered, database):
            raise HTTPException(status_code=409, detail="El lote de prueba ya fue registrado; no se repetirá")
        previews = await asyncio.to_thread(build_rendered_test_previews, database)
        all_pass, _case_results = _all_preview_checks(previews)
        if not all_pass:
            raise HTTPException(status_code=409, detail="Preflight A–E no aprobado; SMTP bloqueado")
        send_result = await asyncio.to_thread(
            run_test_batch, ALL_CASES, test_mode=True, dry_run=False,
            db=database, require_delivery_unknown_baseline=True,
        )
        return _render_panel(
            commit=os.environ.get("RENDER_GIT_COMMIT", "unknown"), secret_present=True,
            mongo_ready=True, smtp_ready=True, test_mode=True, mass_send=False,
            delivery_unknown=send_result.get("delivery_unknown_after"), previews=previews,
            send_result=send_result,
        )
    except TestCampaignCLIError:
        raise HTTPException(status_code=409, detail="Prueba ya ejecutada o precondición no satisfecha") from None
    except HTTPException:
        raise
    except Exception as exc:
        # Do not echo exception messages: a lower layer may contain sensitive
        # config or source details. The type is enough for server-side triage.
        import logging

        logging.getLogger(__name__).error("owner_campaign_admin_test_failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="La prueba se detuvo sin completar el envío") from None
