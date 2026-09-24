"""Admin-only orchestration for the restricted A-E owner-campaign test."""

from __future__ import annotations

import html
import hashlib
import json
import os
import time
from collections import Counter
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import asyncio

from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from .owner_campaign_test_actions import (
    ACCEPT_PRICE_ACTION,
    ADVISOR_ACTION,
    REPORT_ACTION,
    TEST_CAMPAIGN_ID,
    TEST_RECIPIENT,
    issue_test_link_token,
    test_mode_enabled,
)
from .owner_campaign_test_runtime import build_owner_campaign_test_cases_live
from .owner_campaign_test_sender import (
    SERVICE_BASE_URL,
    TEST_LEDGER_COLLECTION,
    TestSenderError,
    prepare_test_messages,
    send_test_messages,
)


ALLOWED_ACTIONS = frozenset({
    "GENERATE_PREVIEWS_ABD",
    "SEND_TEST_EMAILS_ABD",
    "GENERATE_PREVIEWS_CE",
    "SEND_TEST_EMAILS_CE",
    "VERIFY_TEST_EVENTS",
    # Compatibility aliases retained for the existing internal POST client.
    "GENERATE_PREVIEWS",
    "SEND_TEST_EMAILS",
    "VERIFY_TEST_ACTIONS",
})
MASS_SEND_ENV = "OWNER_CAMPAIGN_MASS_SEND_ENABLED"
INITIAL_CASE_IDS = ("A", "B", "D")
REMAINING_CASE_IDS = ("C", "E")
INITIAL_PROPERTY_CODES = frozenset({"5641", "16521", "16527"})
CASE_C_PROPERTY_CODE = "16486"


class TestRunnerError(ValueError):
    """The restricted runner request or environment is not safe to proceed."""


def validate_runner_request(payload: Any, *, test_mode: bool, mass_send_enabled: bool) -> str:
    if not test_mode:
        raise TestRunnerError("test_mode_required")
    if mass_send_enabled:
        raise TestRunnerError("mass_send_must_remain_disabled")
    if not isinstance(payload, Mapping) or set(payload) != {"action"}:
        raise TestRunnerError("runner_body_must_contain_only_action")
    action = str(payload.get("action") or "").strip().upper()
    if action not in ALLOWED_ACTIONS:
        raise TestRunnerError("runner_action_not_allowed")
    return action


def _mass_send_enabled() -> bool:
    return os.getenv(MASS_SEND_ENV, "false").strip().casefold() in {"true", "1", "yes", "on"}


def _safe_case_summary(case: Any) -> dict[str, Any]:
    members = case.portfolio_cases if case.case_id == "E" else (case,)
    return {
        "case_id": case.case_id,
        "property_codes": [str(item.property_code) for item in members],
        "property_count": len(members),
        "operations": [str(item.operation) for item in members],
        "segments": [str(item.evidence_segment) for item in members],
        "document_types": [str(item.document_type) for item in members],
        "cta_types": [str(item.cta_type) for item in members],
    }


def _preview_page(prepared: list[Any], *, phase: str) -> str:
    cards = []
    for item in prepared:
        summary = _safe_case_summary(item.case)
        title = f"Test {html.escape(summary['case_id'])} · {len(summary['property_codes'])} propiedad(es)"
        detail = html.escape(", ".join(summary["property_codes"]))
        source = html.escape(item.html, quote=True)
        height = 1200 if summary["case_id"] != "E" else max(1300, 430 * len(summary["property_codes"]))
        cards.append(
            f'<section class="case"><h2>{title}</h2><p>Códigos: {detail}</p>'
            f'<iframe title="Vista previa {summary["case_id"]}" height="{height}" '
            f'sandbox="allow-same-origin allow-forms allow-popups allow-top-navigation-by-user-activation" '
            f'srcdoc="{source}"></iframe></section>'
        )
    return (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Previews E2E PROCASA</title><style>body{font:14px Arial,sans-serif;margin:0;background:#f3f5f8;color:#172554}"
        ".wrap{max-width:980px;margin:auto;padding:20px}.case{background:white;padding:12px;margin:0 0 24px;border:1px solid #e5e6ef;border-radius:12px}"
        "iframe{width:100%;border:1px solid #e5e6ef;border-radius:8px;background:white}h1{font-size:22px}</style></head><body>"
        f"<main class='wrap'><h1>Previews de prueba · fase {html.escape(phase)}</h1><p>Destinatario fijo: {html.escape(TEST_RECIPIENT)} · sin envío</p>"
        "<section class='case'><h2>Pre-flight</h2><ul>"
        "<li>RENDER_OK: PASS</li><li>DATA_VALIDATION_OK: PASS</li>"
        "<li>SIGNED_URLS_OK: PASS</li><li>RECIPIENT_GUARD_OK: PASS</li>"
        "<li>PRICE_MUTATION_GUARD_OK: PASS (acciones firmadas de test, sin actualización del precio vivo)</li>"
        "</ul></section>"
        + "".join(cards) + "</main></body></html>"
    )


def _runner_db(db: Any = None) -> Any:
    if db is not None:
        return db
    from chatbot.storage import get_db

    return get_db()


def _next_test_phase(db: Any) -> tuple[str, tuple[str, ...]]:
    rows = list(db[TEST_LEDGER_COLLECTION].find({
        "campaign_id": TEST_CAMPAIGN_ID,
        "test_mode": True,
    }))
    if not rows:
        return "INITIAL", INITIAL_CASE_IDS
    if any(str(row.get("actual_recipient_email") or "").strip().casefold() != TEST_RECIPIENT for row in rows):
        raise TestRunnerError("test_campaign_other_recipient_present")
    if any(row.get("delivery_status") != "test_sent" for row in rows):
        raise TestRunnerError("test_campaign_has_incomplete_prior_send")
    codes = [str(row.get("property_code") or "") for row in rows]
    if len(codes) != len(set(codes)):
        raise TestRunnerError("test_campaign_ledger_has_duplicate_properties")
    code_set = set(codes)
    if code_set == INITIAL_PROPERTY_CODES:
        return "REMAINING", REMAINING_CASE_IDS
    if INITIAL_PROPERTY_CODES <= code_set and CASE_C_PROPERTY_CODE in code_set:
        portfolio_codes = code_set - INITIAL_PROPERTY_CODES - {CASE_C_PROPERTY_CODE}
        if len(portfolio_codes) >= 3:
            raise TestRunnerError("test_campaign_all_batches_already_sent")
        raise TestRunnerError("test_campaign_remaining_batch_incomplete")
    raise TestRunnerError("test_campaign_phase_state_unexpected")


def _require_phase(db: Any, expected_phase: str | None) -> tuple[str, tuple[str, ...]]:
    phase, case_ids = _next_test_phase(db)
    if expected_phase is not None and phase != expected_phase:
        raise TestRunnerError("test_campaign_phase_not_ready")
    return phase, case_ids


def generate_previews(db: Any = None, *, expected_phase: str | None = None) -> tuple[str, dict[str, Any]]:
    if not test_mode_enabled() or _mass_send_enabled():
        raise TestRunnerError("unsafe_test_environment")
    database = _runner_db(db)
    phase, case_ids = _require_phase(database, expected_phase)
    cases = build_owner_campaign_test_cases_live(database, case_ids=case_ids)
    prepared = prepare_test_messages(cases)
    if tuple(item.case.case_id for item in prepared) != case_ids:
        raise TestRunnerError("requested_live_previews_incomplete")
    return _preview_page(prepared, phase=phase), {
        "status": "previews_pass",
        "phase": phase,
        "case_ids": list(case_ids),
        "campaign_id": TEST_CAMPAIGN_ID,
        "test_mode": True,
        "actual_recipient_email": TEST_RECIPIENT,
        "cc": [],
        "bcc": [],
        "owner_emails_sent": 0,
        "cases": [_safe_case_summary(item.case) for item in prepared],
    }


def _http_get(url: str, *, timeout: int = 35) -> tuple[int, Mapping[str, str], bytes]:
    request = Request(url, method="GET", headers={"User-Agent": "PROCASA-owner-campaign-test/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), dict(response.headers.items()), response.read(25_000_000)
    except HTTPError as exc:
        return int(exc.code), dict(exc.headers.items()), exc.read(2_000_000)
    except (URLError, TimeoutError, OSError) as exc:
        raise TestRunnerError("public_test_url_unreachable") from exc


def _health_delivery_unknown() -> int:
    status, headers, body = _http_get(f"{SERVICE_BASE_URL}/health", timeout=20)
    if status != 200:
        raise TestRunnerError("health_check_not_200")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TestRunnerError("health_payload_unreadable") from exc
    candidates: list[int] = []
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if str(key).casefold() == "delivery_unknown" and isinstance(child, int) and not isinstance(child, bool):
                    candidates.append(child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(payload)
    # Health intentionally mirrors this metric under chatbot and background
    # worker sections. Accept repeated copies only when they all agree; any
    # disagreement remains fail-closed.
    if not candidates or len(set(candidates)) != 1:
        raise TestRunnerError("delivery_unknown_metric_not_unambiguous")
    return candidates[0]


def send_tests(db: Any = None, *, expected_phase: str | None = None) -> dict[str, Any]:
    if not test_mode_enabled() or _mass_send_enabled():
        raise TestRunnerError("unsafe_test_environment")
    database = _runner_db(db)
    phase, case_ids = _require_phase(database, expected_phase)
    before = _health_delivery_unknown()
    if before > 6:
        raise TestRunnerError("delivery_unknown_baseline_exceeds_safety_limit")
    cases = build_owner_campaign_test_cases_live(database, case_ids=case_ids)
    # Fail closed for this fixed phase before SMTP; later phase cases cannot
    # prevent the user from seeing A/B/D first.
    prepared = prepare_test_messages(cases)
    if tuple(item.case.case_id for item in prepared) != case_ids:
        raise TestRunnerError("requested_test_batch_incomplete")
    results = send_test_messages(cases, db=database)
    if len(results) != len(case_ids) or any(row.get("status") != "sent_to_test_recipient" for row in results):
        raise TestRunnerError("test_delivery_incomplete")
    after = _health_delivery_unknown()
    over_delivery_unknown_limit = after > 6
    return {
        "status": "delivery_unknown_limit_exceeded" if over_delivery_unknown_limit else "test_messages_sent",
        "critical_stop": over_delivery_unknown_limit,
        "phase": phase,
        "case_ids": list(case_ids),
        "campaign_id": TEST_CAMPAIGN_ID,
        "test_mode": True,
        "actual_recipient_email": TEST_RECIPIENT,
        "cc": [],
        "bcc": [],
        "owner_emails_sent": 0,
        "test_emails_sent": len(results),
        "results": results,
        "delivery_unknown_before": before,
        "delivery_unknown_after": after,
        "new_delivery_unknown": max(0, after - 6),
        "mass_send_enabled": False,
    }


def _event_exists(db: Any, *, code: str, event_name: str) -> Mapping[str, Any] | None:
    return db["conversation_events"].find_one({
        "campaign_id": TEST_CAMPAIGN_ID,
        "property_code": code,
        "event_name": event_name,
        "test_mode": True,
    })


def verify_test_actions(db: Any = None) -> dict[str, Any]:
    if not test_mode_enabled() or _mass_send_enabled():
        raise TestRunnerError("unsafe_test_environment")
    if db is None:
        from chatbot.storage import get_db

        db = get_db()
    delivery_unknown_before = _health_delivery_unknown()
    if delivery_unknown_before > 6:
        raise TestRunnerError("delivery_unknown_baseline_exceeds_safety_limit")
    ledger = db[TEST_LEDGER_COLLECTION]
    sent = list(ledger.find({"campaign_id": TEST_CAMPAIGN_ID, "test_mode": True, "actual_recipient_email": TEST_RECIPIENT}))
    by_code = {str(row.get("property_code") or ""): row for row in sent if row.get("delivery_status") == "test_sent"}
    if not {"5641", "16521", "16486", "16527"}.issubset(by_code):
        raise TestRunnerError("test_send_ledger_incomplete")
    group_counts: Counter[str] = Counter(
        str(row.get("intended_owner_email") or "").casefold()
        for code, row in by_code.items() if code not in {"5641", "16521", "16486", "16527"}
    )
    e_owner = next((owner for owner, count in group_counts.items() if owner and count >= 3), None)
    if not e_owner:
        raise TestRunnerError("test_e_ledger_group_missing")
    fixed_codes = {"5641", "16521", "16486", "16527"}
    e_rows = [
        row for row in by_code.values()
        if str(row.get("property_code") or "") not in fixed_codes
        and str(row.get("intended_owner_email") or "").casefold() == e_owner
    ]
    if len(e_rows) < 3:
        raise TestRunnerError("test_e_property_ledger_incomplete")
    e_rows.sort(key=lambda row: str(row.get("property_code") or ""))

    # Capture price from the live property immediately before and after the real public action URL.
    from .owner_campaign_test_actions import _live_price_snapshot, _database

    database = _database(db)
    a_before_operation, live_before = _live_price_snapshot(database, "5641")
    a_token = issue_test_link_token(property_code="5641", action=ACCEPT_PRICE_ACTION)
    b_token = issue_test_link_token(property_code="16521", action=ADVISOR_ACTION)
    a_url = f"{SERVICE_BASE_URL}/campana/test-accion?token={quote(a_token, safe='')}"
    b_url = f"{SERVICE_BASE_URL}/campana/test-accion?token={quote(b_token, safe='')}"
    a_status, a_headers, a_body = _http_get(a_url)
    b_status, b_headers, b_body = _http_get(b_url)
    a_after_operation, live_after = _live_price_snapshot(database, "5641")

    report_type = str(by_code["5641"].get("document_type") or "").upper()
    if report_type not in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}:
        raise TestRunnerError("test_a_report_type_missing")
    report_token = issue_test_link_token(property_code="5641", action=REPORT_ACTION, document_type=report_type)
    report_url = f"{SERVICE_BASE_URL}/campana/informe?token={quote(report_token, safe='')}"
    report_status, report_headers, report_body = _http_get(report_url)

    token_parts = a_token.split(".")
    signature = token_parts[2]
    token_parts[2] = ("A" if signature[0] != "A" else "B") + signature[1:]
    invalid_token = ".".join(token_parts)
    invalid_status, _, _ = _http_get(f"{SERVICE_BASE_URL}/campana/test-accion?token={quote(invalid_token, safe='')}")
    expired_token = issue_test_link_token(
        property_code="5641", action=ADVISOR_ACTION, now_epoch=int(time.time()) - 7200,
    )
    expired_status, _, _ = _http_get(f"{SERVICE_BASE_URL}/campana/test-accion?token={quote(expired_token, safe='')}")
    cross_status, _, _ = _http_get(
        f"{SERVICE_BASE_URL}/campana/test-accion?token={quote(b_token, safe='')}&property_code=5641"
    )
    # The signed token is the only property identity. A conflicting, unsigned
    # query parameter may be ignored (200 for the token's own property), but it
    # must never create an event under the other property's identity.
    wrong_property_event_id = hashlib.sha256(
        f"{TEST_CAMPAIGN_ID}|5641|advisor_review_requested|{b_token}".encode("utf-8")
    ).hexdigest()
    cross_property_token_blocked = (
        cross_status in {200, 400, 404}
        and db["conversation_events"].find_one({"event_id": wrong_property_event_id}) is None
        and bool(_event_exists(db, code="16521", event_name="advisor_review_requested_test"))
    )

    e_action_results = []
    e_report_results = []
    for row in e_rows:
        code = str(row.get("property_code") or "")
        action = ACCEPT_PRICE_ACTION if row.get("cta_type") == "PRICE_AUTHORIZATION" else ADVISOR_ACTION
        token = issue_test_link_token(property_code=code, action=action)
        status, _, _ = _http_get(f"{SERVICE_BASE_URL}/campana/test-accion?token={quote(token, safe='')}")
        expected_event = "price_authorized_test" if action == ACCEPT_PRICE_ACTION else "advisor_review_requested_test"
        stored = _event_exists(db, code=code, event_name=expected_event)
        e_action_results.append(status == 200 and bool(stored))

        report_type = str(row.get("document_type") or "").upper()
        if report_type not in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}:
            e_report_results.append(False)
            continue
        report_token = issue_test_link_token(property_code=code, action=REPORT_ACTION, document_type=report_type)
        status, headers, body = _http_get(
            f"{SERVICE_BASE_URL}/campana/informe?token={quote(report_token, safe='')}"
        )
        opened = _event_exists(db, code=code, event_name="report_opened_test")
        e_report_results.append(
            status == 200
            and headers.get("Content-Type", "").split(";")[0] == "application/pdf"
            and "private" in headers.get("Cache-Control", "")
            and "no-store" in headers.get("Cache-Control", "")
            and len(body) > 0
            and bool(opened)
        )

    auth_event = _event_exists(db, code="5641", event_name="price_authorized_test")
    advisor_event = _event_exists(db, code="16521", event_name="advisor_review_requested_test")
    report_event = _event_exists(db, code="5641", event_name="report_opened_test")
    price_unchanged = a_before_operation == a_after_operation and live_before == live_after
    event_values_match = bool(auth_event) and (
        float(auth_event.get("current_price", 0)) == float(live_before.get("precio_uf", -1))
        and float(auth_event.get("proposed_price", -1)) == float(by_code["5641"].get("display_recommended_price", -2))
    )
    report_resolution = str(report_headers.get("X-Campaign-Report-Resolution") or "")
    delivery_unknown = _health_delivery_unknown()
    if not (a_status == 200 and b_status == 200 and report_status == 200 and report_headers.get("Content-Type", "").split(";")[0] == "application/pdf" and "private" in report_headers.get("Cache-Control", "") and "no-store" in report_headers.get("Cache-Control", "") and len(report_body) > 0):
        raise TestRunnerError("test_public_url_smoke_failed")
    if not (auth_event and advisor_event and report_event and event_values_match and price_unchanged):
        raise TestRunnerError("test_event_or_live_price_validation_failed")
    if invalid_status not in {400, 404} or expired_status not in {400, 404} or not cross_property_token_blocked:
        raise TestRunnerError("invalid_or_cross_property_token_not_blocked")
    if not all(e_action_results) or not all(e_report_results):
        raise TestRunnerError("test_e_property_specific_links_failed")
    over_delivery_unknown_limit = delivery_unknown > 6
    return {
        "status": "delivery_unknown_limit_exceeded" if over_delivery_unknown_limit else "test_actions_verified",
        "critical_stop": over_delivery_unknown_limit,
        "campaign_id": TEST_CAMPAIGN_ID,
        "test_mode": True,
        "auth_url_http": a_status,
        "advisor_url_http": b_status,
        "report_url_http": report_status,
        "report_content_type": report_headers.get("Content-Type", "").split(";")[0],
        "report_cache_control": report_headers.get("Cache-Control"),
        "report_bytes": len(report_body),
        "report_resolution": report_resolution,
        "price_authorization_event_stored": True,
        "advisor_review_event_stored": True,
        "report_opened_event_stored": True,
        "auth_event_current_price": auth_event.get("current_price"),
        "auth_event_proposed_price": auth_event.get("proposed_price"),
        "live_price_before": live_before.get("precio_uf"),
        "live_price_after": live_after.get("precio_uf"),
        "live_property_price_changed": not price_unchanged,
        "invalid_token_blocked": invalid_status in {400, 404},
        "expired_token_blocked": expired_status in {400, 404},
        "cross_property_token_blocked": cross_property_token_blocked,
        "e_property_count": len(e_rows),
        "e_property_ctas_verified": sum(e_action_results),
        "e_property_reports_verified": sum(e_report_results),
        "delivery_unknown_before": delivery_unknown_before,
        "delivery_unknown_after": delivery_unknown,
        "new_delivery_unknown": max(0, delivery_unknown - 6),
        "actual_recipient_email": TEST_RECIPIENT,
        "owner_emails_sent": 0,
    }


def execute_runner_action(action: str, db: Any = None) -> Any:
    normalized = validate_runner_request(
        {"action": action}, test_mode=test_mode_enabled(), mass_send_enabled=_mass_send_enabled(),
    )
    phase_actions = {
        "GENERATE_PREVIEWS_ABD": ("generate", "INITIAL"),
        "SEND_TEST_EMAILS_ABD": ("send", "INITIAL"),
        "GENERATE_PREVIEWS_CE": ("generate", "REMAINING"),
        "SEND_TEST_EMAILS_CE": ("send", "REMAINING"),
    }
    if normalized in {"VERIFY_TEST_EVENTS", "VERIFY_TEST_ACTIONS"}:
        return verify_test_actions(db)
    if normalized == "GENERATE_PREVIEWS":
        return generate_previews(db)
    if normalized == "SEND_TEST_EMAILS":
        return send_tests(db)
    operation, phase = phase_actions[normalized]
    if operation == "generate":
        return generate_previews(db, expected_phase=phase)
    return send_tests(db, expected_phase=phase)


def render_admin_runner_ui() -> HTMLResponse:
    """Return the deliberately small, non-editable test-runner page."""
    recipient = html.escape(TEST_RECIPIENT)
    actions = [
        ("GENERATE_PREVIEWS_ABD", "1. Generar previews A/B/D", "preview", "INITIAL"),
        ("SEND_TEST_EMAILS_ABD", "2. Enviar test A/B/D", "send", "INITIAL"),
        ("GENERATE_PREVIEWS_CE", "3. Generar previews C/E", "preview", "REMAINING"),
        ("SEND_TEST_EMAILS_CE", "4. Enviar test C/E", "send", "REMAINING"),
        ("VERIFY_TEST_EVENTS", "5. Verificar eventos test", "verify", ""),
    ]
    buttons = "".join(
        f'<button type="button" data-action="{action}" data-kind="{kind}" data-phase="{phase}">{label}</button>'
        for action, label, kind, phase in actions
    )
    document = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Runner E2E PROCASA</title><style>
body{{font:15px Arial,sans-serif;margin:0;background:#f4f6fb;color:#172554}}main{{max-width:900px;margin:28px auto;padding:20px}}
section{{background:#fff;border:1px solid #dfe3ef;border-radius:12px;padding:20px;margin-bottom:18px}}
button{{display:block;width:100%;text-align:left;padding:13px 16px;margin:9px 0;border:1px solid #d9dcef;border-radius:8px;background:#fff;color:#172554;font-weight:700;cursor:pointer}}
button:disabled{{opacity:.45;cursor:not-allowed}}button.send{{border-color:#c6b8ef;background:#f6f2ff}}#status{{white-space:pre-wrap;overflow-wrap:anywhere}}
iframe{{width:100%;min-height:420px;border:1px solid #dfe3ef;border-radius:8px;background:white}}.muted{{color:#52617d}}
</style></head><body><main><section><h1>Prueba E2E de campaña</h1>
<p>Modo TEST · destinatario fijo: <strong>{recipient}</strong> · campaña de prueba fija · envío masivo deshabilitado.</p>
<p class="muted">No se admiten destinatarios, códigos ni precios editables. Las acciones de envío usan POST y vuelven a validar todos los datos antes de SMTP.</p>
{buttons}</section><section><h2>Resultado</h2><pre id="status">Esperando acción.</pre><iframe id="preview" title="Previews de campaña" sandbox="allow-same-origin allow-forms allow-popups allow-top-navigation-by-user-activation" hidden></iframe></section>
</main><script>
const statusNode=document.getElementById('status');
const previewNode=document.getElementById('preview');
const buttons=[...document.querySelectorAll('button[data-action]')];
const previewPassed=new Set();
buttons.filter(b=>b.dataset.kind==='send').forEach(b=>b.disabled=true);
function show(value){{statusNode.textContent=typeof value==='string'?value:JSON.stringify(value,null,2);}}
buttons.forEach(button=>button.addEventListener('click',async()=>{{
  const action=button.dataset.action;
  if(button.dataset.kind==='send'&&!window.confirm('Enviar exclusivamente a {recipient}')) return;
  if(button.dataset.kind==='send'&&!previewPassed.has(button.dataset.phase)){{show('Genera y revisa el preview de esta fase antes de enviar.');return;}}
  buttons.forEach(item=>item.disabled=true); show('Procesando acción administrativa…'); previewNode.hidden=true;
  let criticalStop=false;
  try{{
    const response=await fetch('/captacion/test-runner',{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json','Accept':'application/json, text/html'}},body:JSON.stringify({{action}})}});
    const contentType=response.headers.get('content-type')||'';
    if(!response.ok){{const body=await response.text();criticalStop=true;show('ERROR HTTP '+response.status+'\n'+body);return;}}
    if(button.dataset.kind==='preview'){{
      const body=await response.text();previewNode.srcdoc=body;previewNode.hidden=false;
      previewPassed.add(button.dataset.phase);
      buttons.filter(item=>item.dataset.kind==='send'&&item.dataset.phase===button.dataset.phase).forEach(item=>item.disabled=false);
      show('Preview generado. Revisa la vista antes del envío.');
    }}else{{
      const body=await response.json();show(body);
      if(body.critical_stop){{criticalStop=true;show(body);return;}}
    }}
  }}catch(error){{show('Error de comunicación con el runner: '+String(error));}}
  finally{{if(!criticalStop)buttons.filter(item=>item.dataset.kind==='preview'||item.dataset.kind==='verify').forEach(item=>item.disabled=false);}}
}}));
</script></body></html>"""
    return HTMLResponse(document, status_code=200, headers={"Cache-Control": "private, no-store"})


async def handle_admin_runner_request(request: Any, require_admin: Any) -> Any:
    """Use the app's existing admin guard before parsing or running actions."""
    await require_admin(request)
    if not test_mode_enabled():
        raise HTTPException(status_code=404, detail="Runner no disponible")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Solicitud inválida") from exc
    try:
        action = validate_runner_request(
            payload, test_mode=True, mass_send_enabled=_mass_send_enabled(),
        )
        result = await asyncio.to_thread(execute_runner_action, action)
    except TestRunnerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if action in {"GENERATE_PREVIEWS_ABD", "GENERATE_PREVIEWS_CE", "GENERATE_PREVIEWS"}:
        document, _summary = result
        return HTMLResponse(document, status_code=200, headers={"Cache-Control": "private, no-store"})
    return result
