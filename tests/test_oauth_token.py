"""OAuth refresh-token manager: rotation, single-flight, invalid_grant."""

from __future__ import annotations

import threading
import time

from email_waterfall.vendors.oauth_token import MemoryTokenStore, OAuthTokenManager


class FakeResp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        import json

        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_refresh_saves_new_refresh_token() -> None:
    store = MemoryTokenStore(
        {
            "getleads": {
                "client_id": "cid-aaaa",
                "refresh_token": "old-refresh",
            }
        }
    )
    posts = []

    def request_fn(tier, method, url, **kwargs):
        posts.append(kwargs.get("data"))
        return FakeResp(
            200,
            {
                "access_token": "access-1",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            },
        )

    mgr = OAuthTokenManager(
        "getleads",
        store=store,
        client_id="cid-aaaa",
        refresh_token="old-refresh",
        request_fn=request_fn,
    )
    token = mgr.access_token()
    assert token == "access-1"
    saved = store.load("getleads")
    assert saved["refresh_token"] == "rotated-refresh"
    assert saved["access_token"] == "access-1"
    assert posts[0]["grant_type"] == "refresh_token"
    assert posts[0]["resource"] == "https://app.getleads.io/api/mcp"


def test_twenty_threads_one_token_post() -> None:
    store = MemoryTokenStore(
        {"getleads": {"client_id": "cid", "refresh_token": "r1"}}
    )
    posts = []
    lock = threading.Lock()

    def request_fn(tier, method, url, **kwargs):
        with lock:
            posts.append(1)
        time.sleep(0.05)
        return FakeResp(
            200,
            {"access_token": "shared", "refresh_token": "r2", "expires_in": 3600},
        )

    mgr = OAuthTokenManager(
        "getleads",
        store=store,
        client_id="cid",
        refresh_token="r1",
        request_fn=request_fn,
    )
    tokens = []

    def worker():
        tokens.append(mgr.access_token())

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(posts) == 1
    assert set(tokens) == {"shared"}
    assert store.load("getleads")["refresh_token"] == "r2"


def test_invalid_grant_sets_auth_failed_and_disables() -> None:
    store = MemoryTokenStore(
        {"getleads": {"client_id": "cid", "refresh_token": "dead"}}
    )

    def request_fn(tier, method, url, **kwargs):
        return FakeResp(400, {"error": "invalid_grant"})

    mgr = OAuthTokenManager(
        "getleads",
        store=store,
        client_id="cid",
        refresh_token="dead",
        request_fn=request_fn,
    )
    try:
        mgr.access_token()
        raise AssertionError("expected refresh failure")
    except RuntimeError:
        pass
    assert mgr.auth_failed is True
    assert mgr.auth_failed_reason == "invalid_grant"
    from email_waterfall.vendors.getleads import GetLeadsClient

    client = GetLeadsClient(token_manager=mgr)
    assert client.enabled is False
