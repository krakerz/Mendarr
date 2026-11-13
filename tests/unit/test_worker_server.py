import pytest
from fastapi.testclient import TestClient

from mendarr.config import Settings
from mendarr.proxy import ProxyConfigError
from mendarr.worker.backends.camoufox import LaunchedBrowser
from mendarr.worker.server import create_app


class FakeContext:
    def __init__(self, browser):
        self.browser = browser

    async def close(self):
        pass


class FakeBrowser:
    def is_connected(self):
        return True

    async def new_context(self, proxy=None):
        return FakeContext(self)


async def fake_launcher(slot):
    async def close():
        pass

    return LaunchedBrowser(FakeBrowser(), "UA-test", close)


def test_info_and_sessions_crud():
    app = create_app(Settings(slots=1, egress_check_url=""), launcher=fake_launcher, start_displays=False)
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok", "slots": {}}
        info = c.get("/info").json()
        assert info["slots"] == 1 and info["cookie_check"] in ("curl", "browser")
        r = c.post("/sessions", json={"session": "s1"})
        assert r.json() == {"session": "s1", "fresh": True}
        assert c.post("/sessions", json={"session": "s1"}).json()["fresh"] is False
        assert c.get("/sessions").json() == {"sessions": ["s1"]}
        assert c.delete("/sessions/s1").json() == {"existed": True}
        assert c.delete("/sessions/s1").json() == {"existed": False}
        assert c.get("/egress").json()["ip"] == "default"


def test_upstream_proxy_in_info_and_egress():
    settings = Settings(slots=1, proxy_url="up:1080", proxy_type="socks5", egress_check_url="")
    app = create_app(settings, launcher=fake_launcher, start_displays=False)
    with TestClient(app) as c:
        assert c.get("/info").json()["proxy"] == "socks5://up:1080"
        egress = c.get("/egress").json()
        assert egress["ip"] == "default" and egress["proxy"] == "socks5://up:1080"


def test_bad_proxy_fails_create_app():
    with pytest.raises(ProxyConfigError):
        create_app(Settings(slots=1, proxy_url="h"), launcher=fake_launcher, start_displays=False)


def test_egress_answers_while_the_lookup_hangs(monkeypatch):
    import asyncio

    import httpx

    class Hanging:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url):
            await asyncio.sleep(3600)

    monkeypatch.setattr(httpx, "AsyncClient", Hanging)
    settings = Settings(slots=1, egress_check_url="http://lookup.invalid/ip", egress_country_url="")
    app = create_app(settings, launcher=fake_launcher, start_displays=False)
    with TestClient(app) as c:
        r = c.get("/egress")
        assert r.json()["ip"] == "unknown"
