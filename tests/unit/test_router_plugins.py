import sqlite3

import pytest
from fakeworker import UA, FakeWorker, challenge_result, plain_result

from mendarr.config import Settings
from mendarr.core.cookies import CookieCache
from mendarr.core.router import Router, V1Params
from mendarr.plugins.store import PluginStore, RollbackPolicy
from mendarr.rpc import SolveResult, WorkerError

URL = "http://example.com/x"
SRC = 'META = {"backend": "camoufox", "notes": "n"}\n\n\nasync def solve(page, ctx):\n    return None\n'


def params(**kw) -> V1Params:
    base = dict(
        url=URL, method="GET", post_data=None, max_timeout_ms=60000, cookies=[], proxy=None, session=None,
        session_ttl_minutes=None, return_only_cookies=False, return_screenshot=False, disable_media=False,
        wait_s=0,
    )
    base.update(kw)
    return V1Params(**base)


def failing(kind="plugin") -> SolveResult:
    return SolveResult(challenge_seen=True, error=WorkerError(type=kind, detail="boom"), user_agent=UA)


@pytest.fixture
def worker():
    return FakeWorker()


@pytest.fixture
def store(tmp_path):
    s = PluginStore.open(tmp_path, policy=RollbackPolicy(min_samples=3, window=5))
    yield s
    s.close()


@pytest.fixture
def router(worker, store, tmp_path):
    cache = CookieCache.open(tmp_path / "c.db", max_ttl_s=3600)
    yield Router(Settings(cookie_cache=False), worker, cache, store=store)
    cache.close()


def _sent(worker):
    return [r for k, r in worker.calls if k == "solve"][-1]


def _promoted(store):
    rec = store.submit("example.com", SRC)
    return store.promote(rec.scope, rec.version)


async def test_default_spec_sent(router, worker, store):
    worker.solve_results = [plain_result(URL)]
    await router.request(params())
    spec = _sent(worker).plugin
    assert (spec.scope, spec.version) == ("_default", 1)
    assert spec.source == store.get("_default", 1).source


async def test_domain_plugin_sent(router, worker, store):
    _promoted(store)
    worker.solve_results = [plain_result(URL)]
    await router.request(params())
    assert _sent(worker).plugin.scope == "example.com"


async def test_counting(router, worker, store):
    _promoted(store)
    worker.solve_results = [challenge_result(URL)]
    await router.request(params())
    assert store.get("example.com", 1).ok == 1
    worker.solve_results = [plain_result(URL)]
    await router.request(params())
    assert store.get("example.com", 1).ok == 1
    worker.solve_results = [failing()]
    await router.request(params())
    assert store.get("example.com", 1).fail == 1
    for kind in ("navigation", "no_slot"):
        worker.solve_results = [failing(kind)]
        await router.request(params())
    assert store.get("example.com", 1).fail == 1


async def test_auto_rollback_opens_incident(router, worker, store):
    store.promote("example.com", store.submit("example.com", SRC).version)
    store.promote("example.com", store.submit("example.com", SRC).version)
    for _ in range(3):
        worker.solve_results = [failing()]
        await router.request(params())
    assert store.active("example.com").version == 1
    assert store.incidents()[0]["state"] == "open"


async def test_replay_records_nothing(router, worker, store):
    rec = _promoted(store)
    worker.solve_results = [challenge_result(URL)]
    out = await router.replay(URL, rec.spec())
    assert out.ok
    assert _sent(worker).plugin == rec.spec()
    assert store.get("example.com", 1).ok == 0
    assert not [k for k, _ in worker.calls if k == "check"]


async def test_store_error_falls_back(router, worker, store, monkeypatch):
    def boom(_):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(store, "resolve", boom)
    worker.solve_results = [plain_result(URL)]
    out = await router.request(params())
    assert out.ok and _sent(worker).plugin is None
