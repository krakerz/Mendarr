import asyncio
from types import SimpleNamespace

import pytest

from mendarr.api.worker_client import WorkerUnavailable
from mendarr.config import Settings
from mendarr.core.cookies import CookieCache
from mendarr.core.refresher import Refresher
from mendarr.healer.config import ConfigFile
from mendarr.rpc import Cookie, EgressInfo

IP = "203.0.113.7"
NOW = 1_000_000.0


class FakeRouter:
    def __init__(self):
        self.calls = []
        self.results = {}

    async def refresh(self, entry, *, renew_before):
        self.calls.append((entry.domain, renew_before))
        r = self.results.get(entry.domain, "ok")
        if isinstance(r, Exception):
            raise r
        return r


class FakeWorker:
    def __init__(self):
        self.ip = IP
        self.fail = False

    async def egress(self):
        if self.fail:
            raise WorkerUnavailable("down")
        return EgressInfo(ip=self.ip, checked_at=0)


class Manual:
    def __init__(self, active=(), hold=()):
        self._active, self._hold = active, hold

    def sessions(self):
        return [SimpleNamespace(active=True, domain=d) for d in self._active]

    def should_hold(self, d):
        return d in self._hold


@pytest.fixture
def cache(tmp_path):
    c = CookieCache.open(tmp_path / "s.db", max_ttl_s=0, clock=lambda: NOW)
    yield c
    c.close()


def put(cache, domain, *, egress=IP, url="http://x/", last_used=NOW, exp=NOW + 3600):
    c = Cookie(name="cf_clearance", value="t", domain=domain, expires=exp)
    cache.put(domain, egress, "ua", [c], url=url, last_used=last_used)


def make(tmp_path, cache, manual=None, **env):
    router, worker = FakeRouter(), FakeWorker()
    sleeps = []

    async def sleep(s):
        sleeps.append(s)

    r = Refresher(
        Settings(data_dir=str(tmp_path), **env), router, cache, worker, ConfigFile(tmp_path / "mendarr.yml"), manual,
        clock=lambda: NOW, sleep=sleep, jitter=lambda a, b: a,
    )
    return r, router, worker, sleeps


async def test_off_by_default(tmp_path, cache):
    r, router, _, _ = make(tmp_path, cache)
    put(cache, "a.com")
    n = 0

    async def sleep(s):
        nonlocal n
        n += 1
        if n > 3:
            raise asyncio.CancelledError

    r._sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        await r._run()
    assert router.calls == [] and r.next_at is None


async def test_filters(tmp_path, cache):
    put(cache, "ok.com")
    put(cache, "other.com", egress="198.51.100.1")
    put(cache, "idle.com", last_used=NOW - 8 * 86400)
    put(cache, "nourl.com", url="")
    put(cache, "held.com")
    put(cache, "busy.com")
    put(cache, "exp.com", exp=NOW + 1)
    r, router, _, _ = make(tmp_path, cache, Manual(active=["busy.com"], hold=["held.com"]))
    r._clock = lambda: NOW + 5  # exp.com is past its expiry for the refresher
    n = await r.run_round(3600, 7)
    assert n == 1 and [c[0] for c in router.calls] == ["ok.com"]
    assert router.calls[0][1] == NOW + 5 + 3600 + Refresher.POLL_S  # renew window covers a late wake-up


async def test_status_and_persisted(tmp_path, cache):
    put(cache, "a.com")
    r, router, _, sleeps = make(tmp_path, cache)
    router.results["a.com"] = "renewed"
    await r.run_round(3600, 7)
    assert r.status()["a.com"].result == "renewed"
    assert cache.entries()[0].last_check_result == "renewed"
    assert sleeps == [1.0]


async def test_error_isolated(tmp_path, cache):
    put(cache, "a.com")
    put(cache, "b.com")
    r, router, _, _ = make(tmp_path, cache)
    router.results["b.com"] = RuntimeError("boom")
    assert await r.run_round(3600, 7) == 2
    assert {d: s.result for d, s in r.status().items()} == {"a.com": "ok", "b.com": "error"}


@pytest.mark.parametrize("mode", ["down", "unknown"])
async def test_skips_round_without_ip(tmp_path, cache, mode):
    put(cache, "a.com")
    r, router, worker, _ = make(tmp_path, cache)
    if mode == "down":
        worker.fail = True
    else:
        worker.ip = "unknown"
    assert await r.run_round(3600, 7) == 0 and router.calls == []


async def test_loop_first_round_after_delay(tmp_path, cache):
    put(cache, "a.com")
    now = [NOW]
    r, router, _, _ = make(tmp_path, cache, cookie_refresh_interval="1h")
    r._clock = lambda: now[0]
    seen = []

    async def sleep(s):
        seen.append((now[0] - NOW, len(router.calls)))
        now[0] += s
        if len(seen) > 8:
            raise asyncio.CancelledError

    r._sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        await r._run()
    assert len(router.calls) == 1
    assert all(n == 0 for t, n in seen if t < 60)  # nothing before the 60 s first delay
    assert r.next_at is not None and r.next_at > now[0]
