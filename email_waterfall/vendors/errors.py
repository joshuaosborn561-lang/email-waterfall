"""Vendor failure logging. Never log tokens or full lead payloads."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import requests

log = logging.getLogger("email_waterfall.vendors")


def url_path(url: str) -> str:
    return urlsplit(url).path or url


def body_preview(response: requests.Response | None, limit: int = 300) -> str:
    if response is None:
        return ""
    text = getattr(response, "text", None)
    if not text:
        return ""
    return str(text)[:limit]


def log_vendor_failure(
    tier: str,
    url: str,
    *,
    status: int | str | None = None,
    body: str = "",
    error: str = "",
) -> None:
    """WARNING with tier, path (no query), status, and first 300 chars of body."""
    extra = f" err={error}" if error else ""
    log.warning(
        "vendor error tier=%s path=%s status=%s body=%s%s",
        tier,
        url_path(url),
        "transport" if status is None else status,
        (body or "")[:300],
        extra,
    )


def remember_first_error(client: Any, *, body: str = "", error: str = "") -> None:
    """Keep the first vendor error body for get_job_status / tier_stats."""
    if getattr(client, "first_error", None):
        return
    preview = (body or error or "").strip()
    if preview:
        client.first_error = preview[:300]


def bump_errors(client: Any, *, body: str = "", error: str = "") -> int:
    client.errors = int(getattr(client, "errors", 0) or 0) + 1
    remember_first_error(client, body=body, error=error)
    return client.errors


def record_response_failure(
    client: Any,
    url: str,
    response: requests.Response | None,
    *,
    error: str = "",
) -> None:
    preview = body_preview(response)
    bump_errors(client, body=preview, error=error)
    log_vendor_failure(
        getattr(client, "tier", "unknown"),
        url,
        status=None if response is None else response.status_code,
        body=preview,
        error=error or ("transport" if response is None else ""),
    )
