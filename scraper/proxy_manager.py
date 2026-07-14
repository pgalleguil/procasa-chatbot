from __future__ import annotations

import os
import re
from dataclasses import dataclass
from itertools import cycle
from typing import Iterable
from urllib.parse import urlparse

from config import AppConfig


@dataclass(slots=True)
class ProxyChoice:
    url: str

    @property
    def requests_proxies(self) -> dict[str, str]:
        return {"http": self.url, "https": self.url}


class ProxyManager:
    def __init__(self, proxies: Iterable[str] | None = None):
        cleaned = [self.normalize_proxy_url(proxy) for proxy in (proxies or []) if proxy and proxy.strip()]
        self._proxies = cleaned
        self._cycle = cycle(self._proxies) if self._proxies else None

    @classmethod
    def from_config(cls, config: AppConfig) -> "ProxyManager":
        proxies: list[str] = []
        if config.proxy_url:
            proxies.append(config.proxy_url)
        proxies.extend(config.proxy_urls)
        for proxy in load_proxies_from_env():
            if proxy not in proxies:
                proxies.append(proxy)
        return cls(proxies)

    def has_proxies(self) -> bool:
        return bool(self._proxies)

    def next_proxy(self) -> ProxyChoice | None:
        if self._cycle is None:
            return None
        return ProxyChoice(next(self._cycle))

    @staticmethod
    def normalize_proxy_url(proxy_url: str) -> str:
        parsed = urlparse(proxy_url)
        if parsed.scheme:
            return proxy_url
        return f"http://{proxy_url}"


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
    proxies = [None, *load_proxies_from_env()]
    if not proxies:
        return None
    return proxies[min(max(attempt, 0), len(proxies) - 1)]
