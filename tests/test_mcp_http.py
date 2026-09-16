"""Streamable HTTP MCP client — JSON, SSE, 401 refresh, session 404."""

from __future__ import annotations

from email_waterfall.vendors.mcp_http import McpError, McpHttpClient, extract_tool_result, parse_mcp_response


class FakeResp:
    def __init__(self, status: int, payload=None, *, text="", headers=None, content_type="application/json"):
        self.status_code = status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        if payload is not None:
            import json

            self._payload = payload
            self.text = json.dumps(payload)
        else:
            self._payload = None
            self.text = text

    def json(self):
        if self._payload is None:
            import json

            return json.loads(self.text)
        return self._payload


class Tokens:
    def __init__(self):
        self.token = "tok-1"
        self.invalidated = 0

    def access_token(self) -> str:
        return self.token

    def invalidate(self) -> None:
        self.invalidated += 1
        self.token = f"tok-{self.invalidated + 1}"


def _client(request_fn, tokens=None) -> McpHttpClient:
    return McpHttpClient(
        url="https://app.getleads.io/api/mcp",
        token_manager=tokens or Tokens(),
        request_fn=request_fn,
    )


def test_parse_plain_json_result() -> None:
    resp = FakeResp(200, {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}})
    assert parse_mcp_response(resp, 7)["result"]["ok"] is True


def test_parse_sse_matching_id_not_first() -> None:
    text = (
        "event: message\n"
        "data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"wrong\":true}}\n"
        "\n"
        "event: message\n"
        "data: {\"jsonrpc\":\"2.0\",\"id\":9,\"result\":{\"structuredContent\":{\"email\":\"a@b.com\"}}}\n"
        "\n"
    )
    resp = FakeResp(200, text=text, content_type="text/event-stream")
    rpc = parse_mcp_response(resp, 9)
    out = extract_tool_result(rpc)
    assert out["email"] == "a@b.com"


def test_structured_content_vs_text_json() -> None:
    structured = extract_tool_result(
        {"result": {"structuredContent": {"email": "s@x.com"}}}
    )
    assert structured["email"] == "s@x.com"
    text_json = extract_tool_result(
        {
            "result": {
                "content": [{"type": "text", "text": "{\"email\":\"t@x.com\"}"}]
            }
        }
    )
    assert text_json["email"] == "t@x.com"


def test_is_error_raises() -> None:
    try:
        extract_tool_result({"result": {"isError": True, "content": []}})
        raise AssertionError("expected McpError")
    except McpError as exc:
        assert exc.is_tool_error is True


def test_401_invalidates_then_retries() -> None:
    tokens = Tokens()
    seen_auth: list[str] = []

    def request_fn(tier, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        seen_auth.append(headers.get("Authorization", ""))
        payload = kwargs.get("json") or {}
        method_name = payload.get("method")
        if method_name == "initialize":
            return FakeResp(
                200,
                {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"protocolVersion": "2025-03-26"}},
                headers={"Mcp-Session-Id": "sess-1"},
            )
        if method_name == "notifications/initialized":
            return FakeResp(202, {})
        if seen_auth[-1] == "Bearer tok-1" and method_name == "tools/call":
            return FakeResp(401, text="unauthorized")
        return FakeResp(
            200,
            {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"structuredContent": {"email": "ok@x.com"}},
            },
        )

    client = _client(request_fn, tokens)
    out = client.call_tool("find_work_email", {"first_name": "A"})
    assert out["email"] == "ok@x.com"
    assert tokens.invalidated == 1
    assert "Bearer tok-2" in seen_auth


def test_session_404_reinitializes_then_succeeds() -> None:
    inits = {"n": 0}

    def request_fn(tier, method, url, **kwargs):
        payload = kwargs.get("json") or {}
        method_name = payload.get("method")
        if method_name == "initialize":
            inits["n"] += 1
            return FakeResp(
                200,
                {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"protocolVersion": "2025-03-26"}},
                headers={"Mcp-Session-Id": f"sess-{inits['n']}"},
            )
        if method_name == "notifications/initialized":
            return FakeResp(202, {})
        if method_name == "tools/call" and inits["n"] == 1:
            return FakeResp(404, text="session expired")
        return FakeResp(
            200,
            {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"structuredContent": {"ok": True}},
            },
        )

    client = _client(request_fn)
    out = client.call_tool("x", {})
    assert out["ok"] is True
    assert inits["n"] == 2
