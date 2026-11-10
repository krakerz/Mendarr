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
