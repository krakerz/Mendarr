import pytest

from mendarr.config import Settings
from mendarr.rpc import CheckRequest, ProxySpec
from mendarr.worker.check import CookieChecker


def make_checker(monkeypatch):
    pytest.importorskip("curl_cffi")
    import curl_cffi.requests

    seen: list = []

    class FakeSession:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def request(self, method, url, **kw):
            seen.append(kw["proxy"])
            raise Exception("curl: (7) Failed to connect to up port 1080")

    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", FakeSession)
    checker = CookieChecker(Settings(proxy_url="up:1080", proxy_type="socks5"), None)
    checker.mode, checker.target = "curl", "chrome"
    return checker, seen


def req(**kw):
    return CheckRequest(url="https://example.com/", cookies=[], user_agent="UA", **kw)


async def test_default_proxy_used_and_named_on_failure(monkeypatch):
    checker, seen = make_checker(monkeypatch)
    res = await checker.check(req())
    assert seen == ["socks5h://up:1080"]
    assert res.error.type == "navigation"
    assert res.error.detail.startswith("Upstream proxy socks5://up:1080 failed")


async def test_request_proxy_wins(monkeypatch):
    checker, seen = make_checker(monkeypatch)
    await checker.check(req(proxy=ProxySpec(url="http://other:3128")))
    assert seen == ["http://other:3128"]
