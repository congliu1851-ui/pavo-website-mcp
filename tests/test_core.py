import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest

from pavo_website_mcp.store import Store, OperationUnknown
from pavo_website_mcp.client import Client, APIError


def test_private_session_and_redaction(tmp_path):
    store = Store(tmp_path / "state")
    store.save_session("secret-token", {"id": "1", "phone_number": "13800138000"})
    assert store.session()["access_token"] == "secret-token"
    assert "secret-token" not in json.dumps(store.status())
    assert "13800138000" not in json.dumps(store.status())
    assert os.stat(store.root).st_mode & 0o777 == 0o700
    assert os.stat(store.root / "session.json").st_mode & 0o777 == 0o600


def test_idempotency_and_mismatch(tmp_path):
    store = Store(tmp_path)
    calls = []
    action = lambda rid: calls.append(rid) or {"task_id": "123"}
    assert store.operation("同一操作", "submit", {"a": 1}, action) == {"task_id": "123"}
    assert store.operation("同一操作", "submit", {"a": 1}, action) == {"task_id": "123"}
    assert len(calls) == 1
    with pytest.raises(ValueError, match="不同"):
        store.operation("同一操作", "submit", {"a": 2}, action)


def test_concurrent_call_is_not_submitted_twice(tmp_path):
    store = Store(tmp_path)
    started, release = Event(), Event()
    def action(rid):
        started.set()
        assert release.wait(5)
        return {"task_id": "t1"}
    with ThreadPoolExecutor() as pool:
        running = pool.submit(store.operation, "once", "submit", {}, action)
        assert started.wait(5)
        try:
            with pytest.raises(OperationUnknown):
                store.operation("once", "submit", {}, action)
        finally:
            release.set()
        assert running.result() == {"task_id": "t1"}


def test_timeout_stays_unknown_after_restart(tmp_path):
    store = Store(tmp_path)
    def timeout(rid):
        raise TimeoutError("不得将此网络错误当成未提交")
    with pytest.raises(OperationUnknown):
        store.operation("uncertain", "submit", {}, timeout)
    store = Store(tmp_path)
    with pytest.raises(OperationUnknown):
        store.operation("uncertain", "submit", {}, lambda rid: pytest.fail("不能重投"))
    assert store.get_operation("uncertain")["status"] == "UNKNOWN"


def test_http_envelope_auth_and_login_redaction(tmp_path):
    store = Store(tmp_path)
    store.save_session("top-secret", {})
    def route(req):
        assert req.url.host == "api.pavo-ai.cn"
        assert req.headers["authorization"] == "Bearer top-secret"
        assert req.headers["x-platform"] == "1"
        return httpx.Response(200, json={"code": "000000", "data": {"ok": True}})
    client = Client(store, transport=httpx.MockTransport(route))
    assert client.request("GET", "/api/v1/test") == {"ok": True}
    with pytest.raises(ValueError):
        client.request("GET", "https://evil.example/")


@pytest.mark.parametrize("response", [
    httpx.Response(401, json={"code": "000501", "message": "top-secret"}),
    httpx.Response(200, json={"code": "BAD", "message": "top-secret"}),
    httpx.Response(502, text="secret server html"),
])
def test_http_errors_never_echo_secret(tmp_path, response):
    store = Store(tmp_path)
    store.save_session("top-secret", {})
    client = Client(store, transport=httpx.MockTransport(lambda r: response))
    with pytest.raises(APIError) as e:
        client.request("GET", "/api/test")
    assert "top-secret" not in str(e.value)
    assert "secret server html" not in str(e.value)


def test_node_resource_lock_serializes_other_threads(tmp_path):
    from threading import Event
    store, ready, entered = Store(tmp_path), Event(), Event()
    def contender():
        ready.set()
        with Store(tmp_path).resource_lock("same-node"):
            entered.set()
    with ThreadPoolExecutor() as pool:
        with store.resource_lock("same-node"):
            work = pool.submit(contender)
            assert ready.wait(3)
            assert not entered.wait(0.05)
        work.result(timeout=3)
        assert entered.is_set()


def test_receipt_encoding_failure_after_action_is_unknown(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(OperationUnknown):
        store.operation("bad-receipt", "write", {}, lambda rid: {"server_created": True, "estimate": float("nan")})
    assert store.get_operation("bad-receipt")["status"] == "UNKNOWN"
