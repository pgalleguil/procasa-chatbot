from __future__ import annotations

import json
from pathlib import Path

from scrapers.scraper_yapo.audit_contextual_filter import audit_html_files


def test_blocked_html_is_counted_as_technical_and_never_commercial(tmp_path):
    blocked = []
    for index in range(11):
        path = tmp_path / f"blocked{index}.html"
        path.write_text("<!doctype html><html><body>Access denied " + ("blocked " * 40) + "</body></html>", encoding="utf-8")
        blocked.append(path)
    good = tmp_path / "good.html"
    good.write_text("<!doctype html><html><body><h1>Trato directo con dueño</h1>" + ("Propiedad disponible. " * 20) + "</body></html>", encoding="utf-8")
    report = audit_html_files([*blocked, good])
    summary = report["summary"]
    assert summary["technical_status_counts"]["BLOCKED"] == 11
    assert summary["commercial_classifications_counted"] == 1
    assert summary["INCIERTO"] == 1
    rows = dict(zip([r[0] for r in report["cases"]], report["cases"]))
    assert all(row[4] is None for row in report["cases"] if row[1] == "BLOCKED")


def test_report_has_no_advertiser_text_fields(tmp_path):
    path = tmp_path / "one.html"
    path.write_text("<!doctype html><html><body><h1>Dueño vende inmueble</h1><div>" + ("Contacto privado. " * 20) + "</div></body></html>", encoding="utf-8")
    report = audit_html_files([path])
    rendered = json.dumps(report, ensure_ascii=False)
    assert "Contacto privado" not in rendered
    assert "Dueño vende inmueble" not in rendered
    assert report["external_io"] == {
        "yapo_requests": 0, "proxy_requests": 0, "mongodb_reads": 0,
        "mongodb_writes": 0, "deepseek_calls": 0, "deployments": 0,
    }
