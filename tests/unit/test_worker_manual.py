import asyncio
import socketserver
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mendarr.config import Settings
from mendarr.worker.backends.camoufox import LaunchedBrowser
from mendarr.worker.server import create_app


class EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        while data := self.request.recv(4096):
            self.request.sendall(data)


@pytest.fixture
def echo_port():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), EchoHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


class FakeBrowser:
    def is_connected(self):
        return True


async def fake_launcher(slot):
    async def close():
        pass

    return LaunchedBrowser(FakeBrowser(), "UA-test", close)


@pytest.fixture
def client(echo_port):
    app = create_app(
        Settings(worker_token="t", slots=1, egress_check_url=""),
        launcher=fake_launcher,
        start_displays=False,
        vnc_port_of=lambda s: echo_port,
    )
    with TestClient(app) as c:
        yield c, app


AUTH = {"Authorization": "Bearer t"}


def test_manual_unknown_session(client):
    c, _ = client
    assert c.get("/manual/nope", headers=AUTH).status_code == 404
    assert c.post("/manual/nope/finish", headers=AUTH).status_code == 404
    assert c.delete("/manual/nope", headers=AUTH).json() == {"existed": False}
    assert c.get("/manual/nope").status_code == 401


def test_vnc_requires_token(client):
    c, _ = client
    with pytest.raises(WebSocketDisconnect) as e, c.websocket_connect("/vnc/1"):
        pass
    assert e.value.code == 4401


def test_vnc_requires_session(client):
    c, _ = client
    with pytest.raises(WebSocketDisconnect) as e, c.websocket_connect("/vnc/1", headers=AUTH):
        pass
    assert e.value.code == 4404


def test_vnc_bridges_bytes(client, monkeypatch):
    c, app = client
    monkeypatch.setattr(app.state.manual, "for_slot", lambda slot: SimpleNamespace(ended=asyncio.Event()))
    with c.websocket_connect("/vnc/1", headers=AUTH) as ws:
        ws.send_bytes(b"RFB 003.008\n")
        assert ws.receive_bytes() == b"RFB 003.008\n"


def test_autosolve_unknown_session_and_headless_backend(client):
    c, _ = client
    assert c.post("/manual/nope/autosolve", headers=AUTH, json={}).status_code == 404
    r = c.post("/manual", headers=AUTH, json={"url": "https://example.org/", "backend": "obscura"})
    assert r.status_code == 400 and "headless" in r.json()["detail"]
    assert c.post("/manual", headers=AUTH, json={"url": "https://example.org/", "backend": "nope"}).status_code == 400


async def _noop():
    pass


def _fake_session(app, calls, gate=None):
    from mendarr.rpc import Snapshot
    from mendarr.worker.demo import DemoRecorder

    class Page:
        url = "https://example.org/"

        def is_closed(self):
            return False

    class Executor:
        async def solve_live(self, spec, page, tracker, slot, timeout_s):
            calls.append(("run", s.demo.paused))
            if gate is not None:
                await gate.wait()
            return "_default@builtin", 2, None

    s = SimpleNamespace(
        id="m1", slot=1, page=Page(), context=SimpleNamespace(pages=[], close=_noop), tracker=None,
        ended=asyncio.Event(), tasks=[], stack=SimpleNamespace(aclose=_noop),
        demo=DemoRecorder(), challenge_seen=False, solving=False,
    )
    app.state.manual._sessions["m1"] = s
    app.state.manual.executor = Executor()
    return s, Snapshot


def test_autosolve_runs_plugin_with_demo_paused_and_judges(client, monkeypatch):
    c, app = client
    calls = []
    s, Snapshot = _fake_session(app, calls)

    async def fake_snap(sess, page, timeout):
        return Snapshot(url="https://example.org/", status=200, title="Hi", html="<html>ok</html>", headers={}, cookies=[])

    monkeypatch.setattr(app.state.manual, "_snap", fake_snap)
    r = c.post("/manual/m1/autosolve", headers=AUTH, json={"timeout_s": 5})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["clicks"] == 2
    assert calls == [("run", True)]
    assert s.demo.paused is False and s.solving is False
    assert "m1" in app.state.manual._sessions  # stays open


def test_autosolve_concurrent_is_409(client):
    c, app = client
    s, _ = _fake_session(app, [])
    s.solving = True
    assert c.post("/manual/m1/autosolve", headers=AUTH, json={}).status_code == 409
