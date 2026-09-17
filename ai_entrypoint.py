"""Private capability used to ensure DeepSeek is called through one service."""
from __future__ import annotations


_CLASSIFICATION_SERVICE_TOKEN = object()


def classification_service_token():
    return _CLASSIFICATION_SERVICE_TOKEN


def is_authorized(token) -> bool:
    return token is _CLASSIFICATION_SERVICE_TOKEN
