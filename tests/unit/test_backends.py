import pytest

from mendarr.worker.backends import BackendUnavailable, error_types
from mendarr.worker.backends.camoufox import LaunchedBrowser
from mendarr.worker.slots import SlotPool


class FakeBrowser:
    def __init__(self, name):
        self.name = name

    def is_connected(self):
        return True


def fake_launcher(name, calls):
    async def launcher(slot):
        calls.append((name, slot))

        async def close():
            pass

        return LaunchedBrowser(FakeBrowser(name), f"UA-{name}", close)

    return launcher


async def test_browser_per_backend():
    calls: list = []
    pool = SlotPool(1, {"camoufox": fake_launcher("c", calls), "patchright": fake_launcher("p", calls)})
    assert (await pool.browser(1)).name == "c"
    assert (await pool.browser(1, "patchright")).name == "p"
    await pool.browser(1)
    await pool.browser(1, "patchright")
    assert calls == [("c", 1), ("p", 1)]
    assert pool.user_agent(1) == "UA-c"
    assert pool.user_agent(1, "patchright") == "UA-p"
    await pool.close()


async def test_unknown_backend():
    pool = SlotPool(1, {"camoufox": fake_launcher("c", [])})
    with pytest.raises(BackendUnavailable):
        await pool.browser(1, "patchright")


async def test_plain_callable_is_camoufox():
    pool = SlotPool(1, fake_launcher("c", []))
    assert (await pool.browser(1)).name == "c"
    with pytest.raises(BackendUnavailable):
        await pool.browser(1, "patchright")


def test_error_types_camoufox():
    pytest.importorskip("playwright")
    from playwright.async_api import Error, TimeoutError

    assert error_types("camoufox") == (Error, TimeoutError)


class FakePage:
    async def evaluate(self, expr):
        return "UA"

    async def close(self):
        pass


class FakeLaunchedBrowser:
    async def new_page(self):
        return FakePage()


def fake_camoufox(kwargs_log):
    class FakeCamoufox:
        def __init__(self, **kw):
            kwargs_log.append(kw)

        async def __aenter__(self):
            return FakeLaunchedBrowser()

        async def __aexit__(self, *a):
            return None

    return FakeCamoufox


async def test_camoufox_upstream_launch(monkeypatch):
    pytest.importorskip("camoufox")
    import camoufox.async_api

    from mendarr.config import Settings
    from mendarr.worker.backends import camoufox as cf

    seen: list = []
    monkeypatch.setattr(camoufox.async_api, "AsyncCamoufox", fake_camoufox(seen))

    async def exit_ip():
        return "5.6.7.8"

    settings = Settings(proxy_url="up:1080", proxy_type="socks5", geoip=True)
    await cf.launch(1, settings=settings, exit_ip=exit_ip)
    assert seen[0]["proxy"] == {"server": "socks5://up:1080"}
    assert seen[0]["firefox_user_prefs"]["network.proxy.failover_direct"] is False
    assert seen[0]["geoip"] == "5.6.7.8"


async def test_camoufox_unknown_exit_ip_raises(monkeypatch):
    pytest.importorskip("camoufox")
    import camoufox.async_api

    from mendarr.config import Settings
    from mendarr.worker.backends import UpstreamUnreachable
    from mendarr.worker.backends import camoufox as cf

    seen: list = []
    monkeypatch.setattr(camoufox.async_api, "AsyncCamoufox", fake_camoufox(seen))

    async def exit_ip():
        return "unknown"

    settings = Settings(proxy_url="up:1080", proxy_type="socks5", geoip=True)
    with pytest.raises(UpstreamUnreachable, match="socks5://up:1080"):
        await cf.launch(1, settings=settings, exit_ip=exit_ip)
    assert seen == []


async def test_camoufox_direct_launch(monkeypatch):
    pytest.importorskip("camoufox")
    import camoufox.async_api

    from mendarr.config import Settings
    from mendarr.worker.backends import camoufox as cf

    seen: list = []
    monkeypatch.setattr(camoufox.async_api, "AsyncCamoufox", fake_camoufox(seen))
    settings = Settings(proxy_url="", geoip=False)
    await cf.launch(1, settings=settings)
    assert "proxy" not in seen[0] and "firefox_user_prefs" not in seen[0]
    assert seen[0]["geoip"] is settings.geoip


def fake_playwright(launch_kw):
    class Chromium:
        async def launch(self, **kw):
            launch_kw.append(kw)
            return FakeLaunchedBrowser()

    class PW:
        chromium = Chromium()

        async def stop(self):
            pass

    class Starter:
        async def start(self):
            return PW()

    return lambda: Starter()


async def test_patchright_proxy_launch(monkeypatch):
    pytest.importorskip("patchright")
    import patchright.async_api

    from mendarr.config import Settings
    from mendarr.worker.backends import patchright as pr

    seen: list = []
    monkeypatch.setattr(patchright.async_api, "async_playwright", fake_playwright(seen))
    flag = "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
    await pr.launch(1, settings=Settings(proxy_url="up:3128"))
    assert seen[0]["proxy"] == {"server": "http://up:3128"}
    assert flag in seen[0]["args"]
    await pr.launch(1, settings=Settings(proxy_url=""))
    assert "proxy" not in seen[1] and flag not in seen[1]["args"]
