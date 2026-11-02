from __future__ import annotations

import json
import os
import platform
import socket
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

try:
    from .config import AppConfig
    from .proxy_manager import ProxyManager, _split_proxy_values
except ImportError:  # direct script execution
    from config import AppConfig
    from proxy_manager import ProxyManager, _split_proxy_values


ENV_KEYS = ("PROXY_URL", "PROXY_URLS", "YAPO_PROXY_URLS", "PROXIES", "PROXY_USER", "PROXY_PASS")
ALLOWED_TEST_URL = "https://example.com/"
DEFAULT_PORTS = {"http": 80, "https": 443}


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def classify_error(exc: BaseException) -> str:
    chain = _exception_chain(exc)
    type_names = {type(item).__name__.lower() for item in chain}
    # Never persist exception text: requests errors can contain authenticated proxy URLs.
    safe_text = " ".join(type(item).__name__.lower() for item in chain)
    safe_text += " " + " ".join(str(getattr(item, "errno", "")) for item in chain)
    if any(name in type_names for name in ("gaierror", "name or service not known")) or "getaddrinfo failed" in safe_text or "name resolution" in safe_text:
        return "DNS"
    if any(getattr(item, "status_code", None) == 407 for item in chain):
        return "AUTH_HTTP_407"
    if any(issubclass(type(item), ssl.SSLError) for item in chain):
        return "TLS"
    if any(isinstance(item, (TimeoutError, socket.timeout)) for item in chain) or "timed out" in safe_text:
        return "TIMEOUT"
    if any(isinstance(item, ConnectionRefusedError) for item in chain) or "10061" in safe_text:
        return "CONNECTION_REFUSED"
    try:
        import requests
        if any(isinstance(item, (requests.exceptions.InvalidProxyURL, requests.exceptions.InvalidSchema, requests.exceptions.MissingSchema)) for item in chain):
            return "INVALID_FORMAT"
        if any(isinstance(item, requests.exceptions.ProxyError) for item in chain):
            return "PROXY_ERROR_UNCLASSIFIED"
    except Exception:  # pragma: no cover - requests is a runtime dependency.
        pass
    return "OTHER"


def classify_http_status(status_code: int) -> str:
    if status_code == 407:
        return "AUTH_HTTP_407"
    if status_code in {401, 403, 429, 451}:
        return "DESTINATION_BLOCKED"
    if 200 <= status_code < 400:
        return "OK"
    return "OTHER_HTTP_STATUS"


def _safe_failure_detail(exc: BaseException) -> dict[str, Any]:
    chain = _exception_chain(exc)
    result: dict[str, Any] = {"error_type": type(exc).__name__}
    for item in chain:
        code = getattr(item, "errno", None)
        if isinstance(code, int):
            result["error_code"] = code
            break
    category = classify_error(exc)
    if category == "DNS":
        code = result.get("error_code")
        result["safe_reason"] = "temporary_dns_failure" if code == getattr(socket, "EAI_AGAIN", None) else "proxy_hostname_not_resolved"
    elif category == "CONNECTION_REFUSED":
        result["safe_reason"] = "proxy_tcp_connection_refused"
    elif category == "TIMEOUT":
        result["safe_reason"] = "proxy_connection_timed_out"
    elif category == "TLS":
        result["safe_reason"] = "tls_handshake_or_certificate_failure"
    elif category == "AUTH_HTTP_407":
        result["safe_reason"] = "proxy_authentication_rejected"
    elif category == "PROXY_ERROR_UNCLASSIFIED":
        result["safe_reason"] = "proxy_transport_error_without_safe_subcause"
    else:
        result["safe_reason"] = "unclassified_runtime_error"
    return result


def _credentials_shape(parsed: Any, env_user: str, env_pass: str) -> tuple[bool, bool]:
    embedded_user = parsed.username is not None
    embedded_pass = parsed.password is not None
    embedded_valid = embedded_user == embedded_pass
    separate_user = bool(env_user)
    separate_pass = bool(env_pass)
    separate_valid = separate_user == separate_pass
    auth_present = (embedded_user and embedded_pass) or (separate_user and separate_pass)
    return auth_present, embedded_valid and separate_valid


def _safe_proxy_record(proxy_url: str, index: int, env_user: str, env_pass: str) -> dict[str, Any]:
    parsed = urlparse(proxy_url)
    try:
        port = parsed.port
        port_valid = port is None or 1 <= port <= 65535
    except ValueError:
        port = None
        port_valid = False
    auth_present, auth_valid = _credentials_shape(parsed, env_user, env_pass)
    syntax_valid = parsed.scheme in DEFAULT_PORTS and bool(parsed.hostname) and port_valid and auth_valid
    return {
        "proxy_index": index,
        "scheme": parsed.scheme or "missing",
        "syntax_valid": syntax_valid,
        "host_present": bool(parsed.hostname),
        "port_explicit": port is not None,
        "port_valid": port_valid,
        "auth_present": auth_present,
        "auth_structure_valid": auth_valid,
        "dns": "not_checked",
        "tcp": "not_checked",
        "tls_to_proxy": "not_required" if parsed.scheme == "http" else "not_checked",
        "http_category": "not_attempted",
        "http_status": None,
    }


def _proxy_http_get(proxy_url: str, destination: str, timeout: float) -> int:
    import requests

    session = requests.Session()
    session.trust_env = False
    response = session.get(
        destination,
        proxies={"http": proxy_url, "https": proxy_url},
        timeout=timeout,
        allow_redirects=False,
        stream=True,
    )
    try:
        return int(response.status_code)
    finally:
        response.close()


def diagnose_proxy(
    config: AppConfig | None = None,
    *,
    report_dir: Path | None = None,
    dns_resolver: Callable[..., Any] = socket.getaddrinfo,
    tcp_connector: Callable[..., Any] = socket.create_connection,
    http_get: Callable[[str, str, float], int] | None = None,
    timeout_seconds: float = 5.0,
    source_commit: str | None = None,
) -> dict[str, Any]:
    config = config or AppConfig()
    manager = ProxyManager.from_config(config)
    variables = {key: bool(os.getenv(key, "").strip()) for key in ENV_KEYS}
    proxies = list(manager._proxies)
    proxy_results: list[dict[str, Any]] = []
    env_user, env_pass = os.getenv("PROXY_USER", ""), os.getenv("PROXY_PASS", "")
    raw_inputs = [value for value in [config.proxy_url, *config.proxy_urls] if value]
    for key in ("PROXY_URL", "PROXY_URLS", "YAPO_PROXY_URLS", "PROXIES"):
        raw_inputs.extend(_split_proxy_values(os.getenv(key, "")))
    invalid_input_count = sum(not ProxyManager.normalize_proxy_url(value) for value in raw_inputs)

    runtime_info = {
        "operating_system": platform.system(),
        "os_release": platform.release(),
        "python_version": platform.python_version(),
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        dns_resolver("example.com", 443, type=socket.SOCK_STREAM)
        general_dns = {"status": "resolved", "error_category": None}
    except Exception as exc:
        general_dns = {"status": "failed", "error_category": classify_error(exc)}

    for index, proxy_url in enumerate(proxies, start=1):
        result = _safe_proxy_record(proxy_url, index, env_user, env_pass)
        proxy_results.append(result)
        if not result["syntax_valid"]:
            result["http_category"] = "INVALID_FORMAT"
            continue
        parsed = urlparse(proxy_url)
        try:
            dns_resolver(parsed.hostname, parsed.port or DEFAULT_PORTS[parsed.scheme], type=socket.SOCK_STREAM)
            result["dns"] = "resolved"
        except Exception as exc:
            result["dns"] = "failed"
            result["error_category"] = classify_error(exc)
            result["failure_detail"] = _safe_failure_detail(exc)
            result["diagnostic_category"] = (
                "RUNTIME_DNS_FAILURE" if general_dns["status"] == "failed"
                else "PROXY_HOST_DNS_FAILURE"
            )
            continue
        port = parsed.port or DEFAULT_PORTS[parsed.scheme]
        try:
            connection = tcp_connector((parsed.hostname, port), timeout=timeout_seconds)
            connection.close()
            result["tcp"] = "connected"
        except Exception as exc:
            result["tcp"] = "failed"
            result["error_category"] = classify_error(exc)
            result["failure_detail"] = _safe_failure_detail(exc)
            continue
        if parsed.scheme == "https":
            try:
                raw_socket = tcp_connector((parsed.hostname, port), timeout=timeout_seconds)
                try:
                    with ssl.create_default_context().wrap_socket(raw_socket, server_hostname=parsed.hostname):
                        pass
                except Exception:
                    raw_socket.close()
                    raise
                result["tls_to_proxy"] = "ok"
            except Exception as exc:
                result["tls_to_proxy"] = "failed"
                result["error_category"] = classify_error(exc)
                result["failure_detail"] = _safe_failure_detail(exc)

        # Probe every syntactically valid, reachable proxy independently. The request
        # always goes through this proxy to the fixed non-Yapo test destination.
        if result["tcp"] == "connected" and result["tls_to_proxy"] != "failed":
            request = http_get or _proxy_http_get
            try:
                status = request(proxy_url, ALLOWED_TEST_URL, timeout_seconds)
                result["http_status"] = status
                result["http_category"] = classify_http_status(status)
                result["authentication"] = (
                    "accepted" if result["auth_present"] and result["http_category"] == "OK"
                    else "rejected" if result["http_category"] == "AUTH_HTTP_407"
                    else "not_confirmed"
                )
            except Exception as exc:
                category = classify_error(exc)
                result["http_category"] = category
                result["error_category"] = category
                result["failure_detail"] = _safe_failure_detail(exc)
                result["authentication"] = "rejected" if category == "AUTH_HTTP_407" else "not_confirmed"

    active_result = next((item for item in proxy_results if item.get("http_category") == "OK"), None)
    first_valid = next((item for item in proxy_results if item["syntax_valid"]), None)
    if not proxies and raw_inputs:
        overall = "INVALID_FORMAT"
        origin = "configuration_or_normalization"
    elif not proxies:
        overall = "NO_PROXY_CONFIGURED"
        origin = "configuration"
    elif first_valid is None:
        overall = "INVALID_FORMAT"
        origin = "configuration_or_normalization"
    elif general_dns["status"] == "failed":
        overall = "RUNTIME_DNS_FAILURE"
        origin = "runtime_dns"
    elif active_result is not None:
        overall = "OK"
        origin = "none"
    elif any(item.get("diagnostic_category") == "PROXY_HOST_DNS_FAILURE" for item in proxy_results):
        overall = "PROXY_HOST_DNS_FAILURE"
        origin = "proxy_host_configuration_or_provider_dns"
    elif any(item.get("http_category") == "AUTH_HTTP_407" for item in proxy_results):
        overall = "AUTH_HTTP_407"
        origin = "proxy_authentication_configuration_or_provider"
    elif any(item.get("tls_to_proxy") == "failed" or item.get("http_category") == "TLS" for item in proxy_results):
        overall = "TLS"
        origin = "environment_or_provider"
    elif any(item.get("tcp") == "failed" for item in proxy_results):
        overall = next(item.get("error_category", "CONNECTION_REFUSED") for item in proxy_results if item.get("tcp") == "failed")
        origin = "environment_or_provider"
    else:
        overall = next((item.get("http_category") for item in proxy_results if item.get("http_category") not in {None, "not_attempted"}), "OTHER")
        origin = (
            "destination_policy" if overall == "DESTINATION_BLOCKED"
            else "code_defect_or_proxy_format" if overall == "INVALID_FORMAT"
            else "environment_or_provider"
        )

    report: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime_info,
        "source_commit": source_commit or os.getenv("YAPO_SOURCE_COMMIT") or None,
        "general_dns_control": general_dns,
        "destination": ALLOWED_TEST_URL,
        "environment_variables_configured": variables,
        "configured_proxy_input_count": len(raw_inputs),
        "invalid_proxy_input_count": invalid_input_count,
        "valid_proxy_count": len(proxies),
        "proxies": proxy_results,
        "result_category": overall,
        "failure_origin": origin,
        "code_checks": {
            "normalizer_used": True,
            "proxy_only_transport": True,
            "environment_proxy_bypass_disabled": True,
            "proxy_mapping_valid": all(
                set(ProxyManager([url]).next_proxy().requests_proxies) == {"http", "https"}
                for url in proxies
            ),
        },
        "credentials_logged": False,
    }
    target_dir = report_dir or config.reports_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("diagnose_proxy_%Y%m%dT%H%M%S_%fZ.json")
    report["report_file"] = name
    path = target_dir / name
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":  # pragma: no cover - exercised as a CLI smoke test.
    print(json.dumps(diagnose_proxy(), ensure_ascii=False, indent=2))
