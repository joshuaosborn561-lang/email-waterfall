#!/usr/bin/env python3
"""One-time getleads OAuth bootstrap (authorization code + PKCE S256).

Run locally in a browser-capable session. There is no client_credentials grant.

  python scripts/getleads_auth.py

Saves client_id + refresh_token to public.ew_vendor_oauth_tokens when Supabase
is configured, otherwise prints env vars to set. Writes docs/getleads_tools.json
from a live tools/list. Prints only the last 4 characters of secrets.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

ISSUER = os.environ.get("GETLEADS_OAUTH_ISSUER", "https://app.getleads.io").rstrip("/")
MCP_URL = os.environ.get("GETLEADS_MCP_URL", "https://app.getleads.io/api/mcp")
RESOURCE = "https://app.getleads.io/api/mcp"
REDIRECT = "http://127.0.0.1:8765/callback"
SCOPE = "mcp:tools offline_access"
DOCS_PATH = ROOT / "docs" / "getleads_tools.json"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _last4(value: str) -> str:
    if not value:
        return "????"
    return value[-4:]


def register_client() -> str:
    resp = requests.post(
        f"{ISSUER}/oauth/register",
        json={
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "email-waterfall",
        },
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    client_id = str(data.get("client_id") or "")
    if not client_id:
        raise SystemExit(f"registration returned no client_id: {data}")
    return client_id


def wait_for_code(expected_state: str) -> str:
    holder: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = parse_qs(parsed.query)
            holder["state"] = (qs.get("state") or [""])[0]
            holder["code"] = (qs.get("code") or [""])[0]
            holder["error"] = (qs.get("error") or [""])[0]
            body = b"You can close this tab. email-waterfall got the OAuth callback."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A003
            return

    server = HTTPServer(("127.0.0.1", 8765), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=300)
    server.server_close()
    if holder.get("error"):
        raise SystemExit(f"OAuth error: {holder['error']}")
    if holder.get("state") != expected_state:
        raise SystemExit("OAuth state mismatch")
    code = holder.get("code") or ""
    if not code:
        raise SystemExit("OAuth callback had no code")
    return code


def exchange_code(client_id: str, code: str, verifier: str) -> dict:
    resp = requests.post(
        f"{ISSUER}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def persist(client_id: str, refresh_token: str, access_token: str = "") -> None:
    from email_waterfall.config import load_settings
    from email_waterfall.vendors.oauth_token import MemoryTokenStore, OAuthTokenManager, SupabaseTokenStore

    cfg = load_settings()
    store = SupabaseTokenStore() if cfg.supabase_configured else MemoryTokenStore()
    mgr = OAuthTokenManager(
        "getleads",
        store=store,
        client_id=client_id,
        refresh_token=refresh_token,
    )
    if access_token:
        mgr._access_token = access_token
        mgr._persist()
    print(
        "saved tokens client_id=…"
        + _last4(client_id)
        + " refresh_token=…"
        + _last4(refresh_token)
        + (" (supabase)" if cfg.supabase_configured else " (memory only — set env vars)")
    )
    if not cfg.supabase_configured:
        print("GETLEADS_CLIENT_ID=" + client_id)
        print("GETLEADS_REFRESH_TOKEN=" + refresh_token)


def write_tools(access_token: str, client_id: str, refresh_token: str) -> int:
    from email_waterfall.vendors.mcp_http import McpHttpClient
    from email_waterfall.vendors.oauth_token import MemoryTokenStore, OAuthTokenManager

    store = MemoryTokenStore(
        {
            "getleads": {
                "client_id": client_id,
                "refresh_token": refresh_token,
                "access_token": access_token,
            }
        }
    )
    mgr = OAuthTokenManager("getleads", store=store, client_id=client_id, refresh_token=refresh_token)
    mgr._access_token = access_token
    mgr._access_expires_at = 10**12
    client = McpHttpClient(url=MCP_URL, token_manager=mgr, tier="getleads")
    tools = client.list_tools()
    DOCS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOCS_PATH.write_text(
        json.dumps({"tools": tools, "mcp_url": MCP_URL}, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {DOCS_PATH} ({len(tools)} tools)")
    return len(tools)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap getleads MCP OAuth")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    client_id = register_client()
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(24)
    authorize = (
        f"{ISSUER}/oauth/authorize?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "scope": SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "resource": RESOURCE,
            }
        )
    )
    print("Open this URL if the browser does not launch:\n" + authorize)
    if not args.no_browser:
        webbrowser.open(authorize)
    code = wait_for_code(state)
    token = exchange_code(client_id, code, verifier)
    refresh = str(token.get("refresh_token") or "")
    access = str(token.get("access_token") or "")
    if not refresh:
        raise SystemExit("token response had no refresh_token — offline_access missing?")
    persist(client_id, refresh, access)
    write_tools(access, client_id, refresh)


if __name__ == "__main__":
    main()
