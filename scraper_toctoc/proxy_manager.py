from __future__ import annotations

import os
import re
from dataclasses import dataclass
from itertools import cycle
from typing import Any, Iterable
from urllib.parse import urlparse, unquote

from config import AppConfig


@dataclass(slots=True)
class ProxyChoice:
    url: str

    @property
    def requests_proxies(self) -> dict[str, str]:
        return {"http": self.url, "https": self.url}

    @property
    def _parsed(self):
        """Parse the proxy URL into components."""
        return urlparse(self.url)

    @property
    def playwright_config(self) -> dict | None:
        """Playwright-native proxy config with separate fields.
        
        Returns {"server": ..., "username": ..., "password": ...} or None.
        Server includes scheme, host, port only.
        Username and password are URL-decoded and sent separately.
        Proxies without auth return only {"server": ...}.
        """
        if not self.url or not self.url.strip():
            return None
        parsed = self._parsed
        config: dict[str, Any] = {}
        # Reconstruct server without credentials: scheme://host:port
        host = parsed.hostname or ""
        port = parsed.port
        scheme = parsed.scheme or "http"
        if port:
            config["server"] = f"{scheme}://{host}:{port}"
        else:
            config["server"] = f"{scheme}://{host}"
        # Add username/password separately if present
        username = parsed.username
        password = parsed.password
        if username:
            config["username"] = unquote(username)
        if password:
            config["password"] = unquote(password)
        return config

    @property
    def safe_url(self) -> str:
        """Return sanitized URL for logging (password masked)."""
        if not self.url:
            return ""
        parsed = self._parsed
        scheme = parsed.scheme or "http"
        host = parsed.hostname or ""
        port = parsed.port
        username = parsed.username
        base = f"{scheme}://{host}" + (f":{port}" if port else "")
        if username:
            return f"{scheme}://{username}:****@{host}" + (f":{port}" if port else "")
        return base

    @property
    def host_port(self) -> str:
        """Extract host:port for logging (never includes password)."""
        parsed = self._parsed
        host = parsed.hostname or ""
        port = parsed.port
        return f"{host}:{port}" if port else host


class ProxyManager:
    def __init__(self, proxies: Iterable[str] | None = None):
        cleaned = [self.normalize_proxy_url(p) for p in (proxies or []) if p and p.strip()]
        self._proxies = cleaned
        self._cycle = cycle(self._proxies) if self._proxies else None

    @classmethod
    def from_config(cls, config: AppConfig) -> "ProxyManager":
        proxies: list[str] = []
        if config.proxy_url:
            proxies.append(config.proxy_url)
        proxies.extend(config.proxy_urls)
        return cls(proxies)

    @classmethod
    def from_env(cls) -> "ProxyManager":
        """Create from environment variables (like load_proxies_from_env)."""
        return cls(load_proxies_from_env())

    def has_proxies(self) -> bool:
        return bool(self._proxies)

    def pool_size(self) -> int:
        return len(self._proxies)

    def next_proxy(self) -> ProxyChoice | None:
        if self._cycle is None:
            return None
        return ProxyChoice(next(self._cycle))

    def get_current_proxy(self) -> ProxyChoice | None:
        """Alias for next_proxy for compatibility."""
        return self.next_proxy()

    @staticmethod
    def normalize_proxy_url(proxy_url: str) -> str:
        parsed = urlparse(proxy_url)
        return proxy_url if parsed.scheme else f"http://{proxy_url}"


def load_proxies(config: AppConfig) -> list[str]:
    return ProxyManager.from_config(config)._proxies


def _split_proxy_values(value: str) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in re.split(r"[,\n;\s]+", value) if p.strip()]


def load_proxies_from_env() -> list[str]:
    raw_values = [
        os.getenv("YAPO_PROXY_URLS", ""),
        os.getenv("PROXY_URLS", ""),
        os.getenv("TOCTOC_PROXY_URLS", ""),
    ]
    proxies_env = os.getenv("PROXIES", "")
    proxy_user = os.getenv("PROXY_USER", "")
    proxy_pass = os.getenv("PROXY_PASS", "")
    if proxies_env:
        legacy = []
        for p in _split_proxy_values(proxies_env):
            if proxy_user and proxy_pass and "@" not in p:
                if "://" in p:
                    scheme, rest = p.split("://", 1)
                    p = f"{scheme}://{proxy_user}:{proxy_pass}@{rest}"
                else:
                    p = f"{proxy_user}:{proxy_pass}@{p}"
            legacy.append(p)
        raw_values.append(",".join(legacy))
    seen = set()
    proxies = []
    for raw in raw_values:
        for proxy in _split_proxy_values(raw):
            if "://" not in proxy:
                proxy = f"http://{proxy}"
            if proxy not in seen:
                seen.add(proxy)
                proxies.append(proxy)
    return proxies


def get_proxy_for_attempt(attempt: int) -> str | None:
    """Like Yapo: attempt 0 = direct (None), then proxies."""
    proxies = [None, *load_proxies_from_env()]
    if not proxies:
        return None
    return proxies[min(max(attempt, 0), len(proxies) - 1)]
