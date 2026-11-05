from __future__ import annotations

import os
import re
import ipaddress
from dataclasses import dataclass
from itertools import cycle
from typing import Iterable
from urllib.parse import quote, urlparse

try:
    from .config import AppConfig
except ImportError:  # direct script execution
    from config import AppConfig


@dataclass(slots=True)
class ProxyChoice:
    url: str
    provider: str = "generic"

    @property
    def requests_proxies(self) -> dict[str, str]:
        return {"http": self.url, "https": self.url}


class ProxyManager:
    def __init__(self, proxies: Iterable[str] | None = None, *, provider: str = "generic"):
        cleaned: list[str] = []
        seen: set[str] = set()
        for proxy in proxies or []:
            if not proxy or not str(proxy).strip():
                continue
            normalized = self.normalize_proxy_url(str(proxy))
            if normalized and normalized not in seen:
                seen.add(normalized)
                cleaned.append(normalized)
        self._proxies = cleaned
        self._cycle = cycle(self._proxies) if self._proxies else None
        self.provider = provider

    @classmethod
    def from_config(cls, config: AppConfig) -> "ProxyManager":
        flashproxy = str(getattr(config, "flashproxy_proxy_url", "") or "").strip()
        if flashproxy:
            return cls([flashproxy], provider="flashproxy")
        config_proxies = [config.proxy_url, *config.proxy_urls]
        username = os.getenv("PROXY_USER", "")
        password = os.getenv("PROXY_PASS", "")
        if username and password:
            config_proxies = [_add_proxy_auth(value, username, password) for value in config_proxies if value]
        return cls([*config_proxies, *load_proxies_from_env()], provider="generic")

    def has_proxies(self) -> bool:
        return bool(self._proxies)

    def next_proxy(self) -> ProxyChoice | None:
        if self._cycle is None:
            return None
        return ProxyChoice(next(self._cycle), provider=self.provider)

    @staticmethod
    def normalize_proxy_url(proxy_url: str) -> str:
        raw = proxy_url.strip()
        if not raw:
            return ""
        candidate = raw if "://" in raw else f"http://{raw}"
        try:
            parsed = urlparse(candidate)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return ""
            host = parsed.hostname
            try:
                ipaddress.ip_address(host)
            except ValueError:
                try:
                    host = host.encode("idna").decode("ascii")
                except UnicodeError:
                    return ""
                if len(host) > 253 or not re.fullmatch(r"(?i)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
                    return ""
                if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in host.split(".")):
                    return ""
            if parsed.port is not None and not (1 <= parsed.port <= 65535):
                return ""
            if (parsed.username is None) != (parsed.password is None):
                return ""
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                return ""
        except ValueError:
            return ""
        return candidate.rstrip("/")


def load_proxies(config: AppConfig) -> list[str]:
    return ProxyManager.from_config(config)._proxies


def _split_proxy_values(value: str) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in re.split(r"[,\n;\s]+", value) if p.strip()]


def _add_proxy_auth(proxy: str, username: str, password: str) -> str:
    if "@" in proxy or not username or not password:
        return proxy
    if "://" in proxy:
        scheme, endpoint = proxy.split("://", 1)
        return f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{endpoint}"
    return f"{quote(username, safe='')}:{quote(password, safe='')}@{proxy}"


def load_proxies_from_env() -> list[str]:
    raw_values = [
        os.getenv("PROXY_URL", ""),
        os.getenv("YAPO_PROXY_URLS", ""),
        os.getenv("PROXY_URLS", ""),
    ]

    proxies_env = os.getenv("PROXIES", "")
    proxy_user = os.getenv("PROXY_USER", "")
    proxy_pass = os.getenv("PROXY_PASS", "")

    raw_values.append(proxies_env)

    seen = set()
    proxies = []

    for raw in raw_values:
        for proxy in _split_proxy_values(raw):
            if proxy_user and proxy_pass:
                proxy = _add_proxy_auth(proxy, proxy_user, proxy_pass)
            proxy = ProxyManager.normalize_proxy_url(proxy)
            if proxy and proxy not in seen:
                seen.add(proxy)
                proxies.append(proxy)

    return proxies


def get_proxy_for_attempt(attempt: int) -> str | None:
    proxies = load_proxies_from_env()
    if not proxies:
        return None
    return proxies[min(max(attempt, 0), len(proxies) - 1)]
