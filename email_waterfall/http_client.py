"""Shared gated HTTP helpers for vendor clients."""

from __future__ import annotations

import requests

from .concurrency import request_with_retry


def post(
    tier: str,
    url: str,
    *,
    json: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
) -> requests.Response | None:
    return request_with_retry(
        tier,
        "POST",
        url,
        json=json,
        headers=headers,
        timeout=timeout,
    )


def get(
    tier: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
) -> requests.Response | None:
    return request_with_retry(
        tier,
        "GET",
        url,
        headers=headers,
        timeout=timeout,
    )
