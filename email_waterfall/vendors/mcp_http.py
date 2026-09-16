"""Synchronous Streamable HTTP MCP client for threaded waterfall workers."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

import requests

from email_waterfall.concurrency import request_with_retry
from email_waterfall.vendors.errors import body_preview, log_vendor_failure

log = logging.getLogger("email_waterfall.vendors.mcp_http")

DEFAULT_PROTOCOL = "2025-03-26"
RequestFn = Callable[..., requests.Response | None]


class McpError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str = "",
        is_tool_error: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.body = body
        self.is_tool_error = is_tool_error


def _sse_messages(text: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    chunks = text.replace("\r\n", "\n").split("\n\n")
    for chunk in chunks:
        data_lines: list[str] = []
        for line in chunk.split("\n"):
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        raw = "\n".join(data_lines).strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            messages.append(parsed)
    return messages


def _rpc_id_match(left: Any, right: Any) -> bool:
    return str(left) == str(right)


def parse_mcp_response(
    response: requests.Response, request_id: Any
) -> dict[str, Any]:
    """Return the JSON-RPC object whose id matches `request_id`."""
    ctype = (response.headers.get("Content-Type") or "").lower()
    text = response.text or ""
    if "text/event-stream" in ctype or text.lstrip().startswith("event:") or "\ndata:" in f"\n{text}":
        messages = _sse_messages(text)
        for msg in messages:
            if _rpc_id_match(msg.get("id"), request_id):
                return msg
        if messages:
            # Last JSON-RPC object as fallback when ids are missing.
            for msg in reversed(messages):
                if "result" in msg or "error" in msg:
                    return msg
        raise McpError("SSE response had no matching JSON-RPC id", status=response.status_code, body=text[:300])
    try:
        data = response.json()
    except ValueError as exc:
        raise McpError("non-JSON MCP response", status=response.status_code, body=text[:300]) from exc
    if isinstance(data, dict):
        return data
    raise McpError("MCP JSON was not an object", status=response.status_code, body=text[:300])


def extract_tool_result(rpc: dict[str, Any]) -> dict[str, Any]:
    if rpc.get("error"):
        err = rpc["error"]
        raise McpError(
            str(err.get("message") or err),
            body=json.dumps(err)[:300],
        )
    result = rpc.get("result")
    if not isinstance(result, dict):
        raise McpError("MCP tools/call missing result object")
    if result.get("isError") is True:
        raise McpError("MCP tool isError", is_tool_error=True, body=json.dumps(result)[:300])
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    if isinstance(structured, list):
        return {"items": structured}
    for item in result.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = str(item.get("text") or "")
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"text": text}
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
        return {"value": parsed}
    return result


class McpHttpClient:
    """One Streamable HTTP session per process, lock around (re)initialize."""

    def __init__(
        self,
        *,
        url: str,
        token_manager: Any,
        tier: str = "getleads",
        timeout: int = 45,
        request_fn: RequestFn | None = None,
        client_name: str = "email-waterfall",
        client_version: str = "1.4.0",
    ):
        self.url = url
        self.token_manager = token_manager
        self.tier = tier
        self.timeout = timeout
        self._request_fn = request_fn or request_with_retry
        self._client_name = client_name
        self._client_version = client_version
        self._lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_id = 1
        self._session_id = ""
        self._protocol = DEFAULT_PROTOCOL
        self._initialized = False

    def _next_rpc_id(self) -> int:
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1
            return rid

    def _headers(self, *, access: str, include_protocol: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if include_protocol:
            headers["MCP-Protocol-Version"] = self._protocol
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post_raw(self, payload: dict[str, Any], *, include_protocol: bool) -> requests.Response | None:
        access = self.token_manager.access_token()
        return self._request_fn(
            self.tier,
            "POST",
            self.url,
            json=payload,
            headers=self._headers(access=access, include_protocol=include_protocol),
            timeout=self.timeout,
        )

    def _log_http(self, response: requests.Response | None, *, extra: str = "") -> None:
        log_vendor_failure(
            self.tier,
            self.url,
            status=None if response is None else response.status_code,
            body=body_preview(response),
            error=extra,
        )

    def initialize(self) -> dict[str, Any]:
        with self._lock:
            return self._initialize_locked()

    def _initialize_locked(self) -> dict[str, Any]:
        rpc_id = self._next_rpc_id()
        payload = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "initialize",
            "params": {
                "protocolVersion": DEFAULT_PROTOCOL,
                "capabilities": {},
                "clientInfo": {
                    "name": self._client_name,
                    "version": self._client_version,
                },
            },
        }
        resp = self._post_raw(payload, include_protocol=False)
        if resp is None or resp.status_code >= 400:
            self._log_http(resp, extra="initialize")
            raise McpError(
                "MCP initialize failed",
                status=None if resp is None else resp.status_code,
                body=body_preview(resp),
            )
        session = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if session:
            self._session_id = session
        rpc = parse_mcp_response(resp, rpc_id)
        result = rpc.get("result") if isinstance(rpc.get("result"), dict) else {}
        negotiated = str(result.get("protocolVersion") or DEFAULT_PROTOCOL)
        self._protocol = negotiated
        notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        notify_resp = self._post_raw(notify, include_protocol=True)
        if notify_resp is not None and notify_resp.status_code >= 400:
            self._log_http(notify_resp, extra="notifications/initialized")
        self._initialized = True
        return result

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._ensure_initialized()
        rpc_id = self._next_rpc_id()
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            payload["params"] = params
        return self._send_with_retry(payload, rpc_id)

    def _send_with_retry(self, payload: dict[str, Any], rpc_id: Any) -> dict[str, Any]:
        retried_auth = False
        retried_session = False
        while True:
            resp = self._post_raw(payload, include_protocol=True)
            if resp is not None and resp.status_code == 401 and not retried_auth:
                self._log_http(resp, extra="401 invalidate+retry")
                self.token_manager.invalidate()
                retried_auth = True
                continue
            had_session = bool(self._session_id)
            if (
                resp is not None
                and resp.status_code == 404
                and had_session
                and not retried_session
            ):
                self._log_http(resp, extra="session 404 re-initialize")
                with self._lock:
                    self._session_id = ""
                    self._initialized = False
                    self._initialize_locked()
                retried_session = True
                continue
            if resp is None or resp.status_code >= 400:
                self._log_http(resp)
                raise McpError(
                    f"MCP HTTP {None if resp is None else resp.status_code}",
                    status=None if resp is None else resp.status_code,
                    body=body_preview(resp),
                )
            return parse_mcp_response(resp, rpc_id)

    def list_tools(self) -> list[dict[str, Any]]:
        rpc = self._rpc("tools/list")
        result = rpc.get("result") if isinstance(rpc.get("result"), dict) else {}
        tools = result.get("tools") or []
        return [t for t in tools if isinstance(t, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        rpc = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return extract_tool_result(rpc)
