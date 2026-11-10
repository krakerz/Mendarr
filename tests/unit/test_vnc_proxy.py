import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_ui_manual import StubManager

from mendarr.api.app import create_app
from mendarr.api.auth import require_user
from mendarr.api.vnc_proxy import upstream_url
from mendarr.config import Settings


def test_upstream_url():
    assert upstream_url("http://mendarr-browser:8192", 2) == "ws://mendarr-browser:8192/vnc/2"
    assert upstream_url("https://w.example:8192/", 1) == "wss://w.example:8192/vnc/1"
    assert upstream_url("http://w:8192/base/", 3) == "ws://w:8192/base/vnc/3"


def test_requires_login(client):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ui/vnc/1"):
            pass
    assert e.value.code == 1008


class FakeUp:
    """Echoes whatever is sent."""

    def __init__(self):
        self.q = []
        self.closed = False

    async def send(self, data):
        self.q.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        while not self.q:
            await asyncio.sleep(0.01)
        return self.q.pop(0)

    async def close(self):
        self.closed = True


class FakeConnect:
    def __init__(self):
        self.up = FakeUp()
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        return self

    async def __aenter__(self):
        return self.up

    async def __aexit__(self, *a):
        return False


def make_client(tmp_path, fake_worker):
    s = Settings(data_dir=str(tmp_path), worker_url="http://wk:8192", worker_token="tok", healer_enabled=False)
    app = create_app(s, worker=fake_worker)
    app.dependency_overrides[require_user] = lambda: "admin"
    return TestClient(app)


def test_no_session(tmp_path, fake_worker):
    with make_client(tmp_path, fake_worker) as c:
        c.app.state.manual = StubManager()
        with pytest.raises(WebSocketDisconnect) as e:
            with c.websocket_connect("/ui/vnc/9"):
                pass
        assert e.value.code == 4404


def test_roundtrip(tmp_path, fake_worker):
    with make_client(tmp_path, fake_worker) as c:
        c.app.state.manual = StubManager()
        fc = FakeConnect()
        c.app.state.vnc_connect = fc
        with c.websocket_connect("/ui/vnc/1", subprotocols=["binary"]) as ws:
            ws.send_bytes(b"RFB 003.008\n")
            assert ws.receive_bytes() == b"RFB 003.008\n"
        url, kw = fc.calls[0]
        assert url == "ws://wk:8192/vnc/1"
        assert kw["additional_headers"]["Authorization"] == "Bearer tok"
