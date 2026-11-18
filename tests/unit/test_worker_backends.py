from fastapi.testclient import TestClient

from mendarr.config import Settings
from mendarr.worker import server
from mendarr.worker.backends.camoufox import LaunchedBrowser
from mendarr.worker.server import create_app


class FakeContext:
    async def close(self):
        pass


class FakeBrowser:
    def is_connected(self):
        return True

    async def new_context(self, proxy=None, **kw):
        return FakeContext()


async def fake_launcher(slot, locale=None):
    async def close():
        pass

    return LaunchedBrowser(FakeBrowser(), "UA-test", close)


def test_backends_endpoints(tmp_path):
    settings = Settings(slots=1, egress_check_url="", worker_token="tok", backends_dir=str(tmp_path))
    app = create_app(settings, launcher=fake_launcher, start_displays=False)
    auth = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        assert c.get("/backends").status_code == 401
        assert c.post("/backends/check").status_code == 401
        body = c.get("/backends", headers=auth).json()
        assert [b["name"] for b in body["backends"]] == ["obscura", "camoufox", "patchright"]
        assert c.post("/backends/nope/update", headers=auth).status_code == 404
        assert c.post("/backends/patchright/revert", headers=auth).status_code == 404
        assert c.post("/backends/restart", json={"force": True}, headers=auth).status_code == 409


def test_forced_restart_closes_sessions_and_restarts_once(monkeypatch, tmp_path):
    kills = []
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: kills.append(sig))
    monkeypatch.setattr(server, "_restart", False)
    settings = Settings(slots=1, egress_check_url="", backends_dir=str(tmp_path))
    app = create_app(settings, launcher=fake_launcher, start_displays=False)
    with TestClient(app) as c:
        c.post("/sessions", json={"session": "s1"})
        app.state.backends._info["camoufox"]["state"] = "pending_restart"
        r = c.post("/backends/restart", json={"force": True})
        assert r.status_code == 200 and r.json()["closed"] == 1
        assert c.get("/sessions").json() == {"sessions": []}
        assert len(kills) == 1 and server.restart_requested()
