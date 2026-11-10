import pytest
from fakeworker import FakeWorker

from mendarr.config import Settings
from mendarr.core.cookies import CookieCache
from mendarr.core.router import Router, V1Params

URL = "http://example.com/x"


def params(**kw) -> V1Params:
    base = dict(
        url=URL, method="GET", post_data=None, max_timeout_ms=60000, cookies=[], proxy=None, session=None,
        session_ttl_minutes=None, return_only_cookies=False, return_screenshot=False, disable_media=False,
        wait_s=0,
    )
    base.update(kw)
    return V1Params(**base)


class StubHold:
    def __init__(self, hold: bool, released: bool) -> None:
        self.hold = hold
        self.released = released
        self.waits: list[tuple[str, float]] = []

    def should_hold(self, domain: str) -> bool:
        return self.hold

    async def wait(self, domain: str, timeout: float) -> bool:
        self.waits.append((domain, timeout))
        return self.released


@pytest.fixture
def worker():
    return FakeWorker()


@pytest.fixture
def cache(tmp_path):
    c = CookieCache.open(tmp_path / "s.db", max_ttl_s=3600)
    yield c
    c.close()


async def test_hold_timeout_does_not_call_worker(worker, cache):
    r = Router(Settings(), worker, cache)
    r.manual = StubHold(True, False)
    out = await r.request(params())
    assert out.ok is False and out.failure == "timeout"
    assert worker.calls == []
    assert r.manual.waits[0][0] == "example.com" and 0 < r.manual.waits[0][1] < 60


async def test_released_hold_runs_normal_flow(worker, cache):
    r = Router(Settings(), worker, cache)
    r.manual = StubHold(True, True)
    out = await r.request(params())
    assert out.ok
    assert len(worker.calls_of("solve")) == 1


async def test_no_hold_never_waits(worker, cache):
    r = Router(Settings(), worker, cache)
    r.manual = StubHold(False, False)
    out = await r.request(params())
    assert out.ok and r.manual.waits == []
