"""OAuth refresh-token manager for the getleads MCP (RFC 8707 resource).

Refresh tokens rotate. Persist the new refresh token before returning an access
token. Guard refreshes with a lock so parallel waterfall workers cannot burn
a rotated token.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urljoin

from email_waterfall.concurrency import request_with_retry
from email_waterfall.config import settings
from email_waterfall.vendors.errors import log_vendor_failure

log = logging.getLogger("email_waterfall.vendors.oauth")

GETLEADS_RESOURCE = "https://app.getleads.io/api/mcp"


class TokenStore(Protocol):
    def load(self, vendor: str) -> dict[str, Any] | None: ...
    def save(self, vendor: str, row: dict[str, Any]) -> None: ...


class MemoryTokenStore:
    def __init__(self, rows: dict[str, dict[str, Any]] | None = None):
        self.rows = rows if rows is not None else {}
        self._lock = threading.Lock()

    def load(self, vendor: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.rows.get(vendor)
            return dict(row) if row else None

    def save(self, vendor: str, row: dict[str, Any]) -> None:
        with self._lock:
            self.rows[vendor] = dict(row)


class SupabaseTokenStore:
    """PostgREST access to public.ew_vendor_oauth_tokens. Service role only."""

    def load(self, vendor: str) -> dict[str, Any] | None:
        from email_waterfall import supabase_sync
        from email_waterfall.config import load_settings

        cfg = load_settings()
        if not cfg.supabase_configured:
            return None
        status, text = supabase_sync.request_on(
            "GET",
            f"ew_vendor_oauth_tokens?vendor=eq.{vendor}&select=*",
            url=cfg.supabase_url,
            key=cfg.supabase_key,
            prefer="return=representation",
        )
        if not text:
            return None
        import json

        data = json.loads(text)
        if isinstance(data, list) and data:
            return data[0] if isinstance(data[0], dict) else None
        if isinstance(data, dict):
            return data
        return None

    def save(self, vendor: str, row: dict[str, Any]) -> None:
        from email_waterfall import supabase_sync
        from email_waterfall.config import load_settings

        cfg = load_settings()
        if not cfg.supabase_configured:
            raise RuntimeError("Supabase not configured; cannot persist OAuth tokens")
        body = {"vendor": vendor, **row}
        supabase_sync.request_on(
            "POST",
            "ew_vendor_oauth_tokens?on_conflict=vendor",
            url=cfg.supabase_url,
            key=cfg.supabase_key,
            body=body,
            prefer="resolution=merge-duplicates,return=minimal",
        )


def _parse_expires_at(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


class OAuthTokenManager:
    def __init__(
        self,
        vendor: str = "getleads",
        *,
        store: TokenStore | None = None,
        issuer: str | None = None,
        resource: str = GETLEADS_RESOURCE,
        client_id: str | None = None,
        refresh_token: str | None = None,
        request_fn=None,
    ):
        self.vendor = vendor
        self.resource = resource
        self.issuer = (issuer or settings.getleads_oauth_issuer).rstrip("/")
        self._store = store if store is not None else self._default_store()
        self._request_fn = request_fn or request_with_retry
        self._lock = threading.Lock()
        self._access_token = ""
        self._access_expires_at = 0.0
        self._refresh_token = refresh_token or settings.getleads_refresh_token
        self._client_id = client_id or settings.getleads_client_id
        self.auth_failed = False
        self.auth_failed_reason: str | None = None
        self._seed_from_store_or_env()

    def _default_store(self) -> TokenStore:
        from email_waterfall.config import load_settings

        if load_settings().supabase_configured:
            return SupabaseTokenStore()
        return MemoryTokenStore()

    def _seed_from_store_or_env(self) -> None:
        row = None
        try:
            row = self._store.load(self.vendor)
        except Exception as exc:
            log.warning("oauth token store load failed vendor=%s err=%s", self.vendor, exc)
        if row:
            self._client_id = str(row.get("client_id") or self._client_id or "")
            self._refresh_token = str(row.get("refresh_token") or self._refresh_token or "")
            self._access_token = str(row.get("access_token") or "")
            expires = _parse_expires_at(row.get("access_expires_at"))
            self._access_expires_at = expires or 0.0
            return
        if self._client_id and self._refresh_token:
            self._persist()

    def _persist(self) -> None:
        expires_iso = None
        if self._access_expires_at:
            expires_iso = datetime.fromtimestamp(
                self._access_expires_at, tz=timezone.utc
            ).isoformat()
        try:
            self._store.save(
                self.vendor,
                {
                    "client_id": self._client_id,
                    "refresh_token": self._refresh_token,
                    "access_token": self._access_token or None,
                    "access_expires_at": expires_iso,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:
            log.warning("oauth token store save failed vendor=%s err=%s", self.vendor, exc)

    @property
    def has_refresh_token(self) -> bool:
        return bool(self._refresh_token and self._client_id) and not self.auth_failed

    @property
    def client_id(self) -> str:
        return self._client_id

    def invalidate(self) -> None:
        with self._lock:
            self._access_token = ""
            self._access_expires_at = 0.0

    def access_token(self) -> str:
        if self.auth_failed:
            raise RuntimeError(self.auth_failed_reason or "getleads auth_failed")
        with self._lock:
            if self._access_token and (self._access_expires_at - time.time()) > 60:
                return self._access_token
            return self._refresh_locked()

    def _refresh_locked(self) -> str:
        if self.auth_failed:
            raise RuntimeError(self.auth_failed_reason or "getleads auth_failed")
        if not self._refresh_token or not self._client_id:
            self.auth_failed = True
            self.auth_failed_reason = "missing_refresh_token"
            raise RuntimeError("getleads missing refresh token")
        token_url = urljoin(self.issuer + "/", "oauth/token")
        resp = self._request_fn(
            self.vendor,
            "POST",
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "resource": self.resource,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )
        if resp is None:
            log_vendor_failure(self.vendor, token_url, error="transport")
            self.auth_failed = True
            self.auth_failed_reason = "refresh_transport_error"
            raise RuntimeError("getleads token refresh failed")
        if resp.status_code in (400, 401):
            log_vendor_failure(
                self.vendor,
                token_url,
                status=resp.status_code,
                body=(resp.text or "")[:300],
            )
            self.auth_failed = True
            reason = "invalid_grant"
            try:
                payload = resp.json()
                reason = str(payload.get("error") or reason)
            except ValueError:
                pass
            self.auth_failed_reason = reason
            raise RuntimeError(f"getleads token refresh failed: {reason}")
        if resp.status_code >= 400:
            log_vendor_failure(
                self.vendor,
                token_url,
                status=resp.status_code,
                body=(resp.text or "")[:300],
            )
            raise RuntimeError(f"getleads token refresh HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError("getleads token refresh returned non-JSON") from exc
        access = str(payload.get("access_token") or "")
        if not access:
            self.auth_failed = True
            self.auth_failed_reason = "missing_access_token"
            raise RuntimeError("getleads token refresh missing access_token")
        new_refresh = str(payload.get("refresh_token") or "")
        if new_refresh:
            self._refresh_token = new_refresh
        expires_in = payload.get("expires_in")
        try:
            ttl = int(expires_in) if expires_in is not None else 3600
        except (TypeError, ValueError):
            ttl = 3600
        self._access_token = access
        self._access_expires_at = time.time() + max(ttl, 1)
        self._persist()
        return self._access_token
