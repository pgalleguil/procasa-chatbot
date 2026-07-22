"""Non-blocking market context from the Banco Central de Chile.

Only official, demonstrable values are returned. A source failure produces an
empty indicator list so the commercial dashboard remains fully operational.
"""

from __future__ import annotations

import re
import threading
import time
from html.parser import HTMLParser
from urllib.request import Request, urlopen


SOURCE_URL = "https://si3.bcentral.cl/Bdemovil/BDE/IndicadoresDiarios"
SOURCE_NAME = "Banco Central de Chile"
_CACHE_TTL_SECONDS = 60 * 60
_cache = {"at": 0.0, "payload": None}
_lock = threading.Lock()


class _IndicatorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_row = False
        self.in_cell = False
        self.in_h3 = False
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.cell: list[str] = []
        self.heading: list[str] = []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "")
        if tag == "tr":
            self.in_row, self.row = True, []
        elif tag == "td" and self.in_row:
            self.in_cell, self.cell = True, []
        elif tag == "h3":
            self.in_h3 = True

    def handle_endtag(self, tag):
        if tag == "td" and self.in_cell:
            value = " ".join("".join(self.cell).split())
            self.row.append(value)
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.row:
                self.rows.append(self.row)
            self.in_row = False
        elif tag == "h3":
            self.in_h3 = False

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)
        if self.in_h3:
            self.heading.append(data)


def _fetch_official_indicators() -> dict:
    request = Request(SOURCE_URL, headers={"User-Agent": "PROCASA-Analytics/2.0"})
    with urlopen(request, timeout=8) as response:
        parser = _IndicatorParser()
        parser.feed(response.read().decode("utf-8", errors="replace"))

    heading = " ".join("".join(parser.heading).split())
    date_match = re.search(r"(\d{2})-(\w{3})-(\d{4})", heading, re.I)
    source_date = date_match.group(0) if date_match else None
    wanted = {
        "Unidad de Fomento (UF)": ("UF", "CLP"),
        "Dólar observado": ("USD/CLP", "CLP"),
        "Tasa de política monetaria (TPM)": ("TPM", "%"),
    }
    indicators = []
    for row in parser.rows:
        if len(row) < 2 or row[0] not in wanted:
            continue
        label, unit = wanted[row[0]]
        indicators.append({
            "key": label.lower().replace("/", "_"),
            "label": label,
            "value": row[1],
            "unit": unit,
            "updated_at": source_date,
            "source": SOURCE_NAME,
            "source_url": SOURCE_URL,
        })
    return {"indicators": indicators, "source": SOURCE_NAME, "source_url": SOURCE_URL, "stale": False}


def get_market_indicators() -> dict:
    now = time.monotonic()
    with _lock:
        if _cache["payload"] is not None and now - _cache["at"] < _CACHE_TTL_SECONDS:
            return _cache["payload"]
    try:
        payload = _fetch_official_indicators()
    except Exception:
        payload = {"indicators": [], "source": SOURCE_NAME, "source_url": SOURCE_URL, "stale": True}
    with _lock:
        _cache.update(at=now, payload=payload)
    return payload
