import pytest
from fakeworker import FakeWorker, UA, challenge_result, plain_result, snapshot

from mendarr.config import Settings
from mendarr.core.cookies import CookieCache
from mendarr.core.router import Router, V1Params
from mendarr.rpc import CheckResult, ProxySpec, WorkerError

URL = "http://example.com/x"


def params(**kw) -> V1Params:
    base = dict(
        url=URL, method="GET", post_data=None, max_timeout_ms=60000, cookies=[], proxy=None, session=None,
        session_ttl_minutes=None, return_only_cookies=False, return_screenshot=False, disable_media=False,
        wait_s=0,
    )
    base.update(kw)
    return V1Params(**base)


@pytest.fixture
def worker():
    return FakeWorker()


@pytest.fixture
def cache(tmp_path):
    c = CookieCache.open(tmp_path / "s.db", max_ttl_s=3600)
    yield c
    c.close()


@pytest.fixture
def router(worker, cache):
    return Router(Settings(), worker, cache)


async def test_cache_miss_solves(router, worker):
    out = await router.request(params())
    assert out.ok and out.message == "Challenge not detected!"
    assert len(worker.calls_of("solve")) == 1
    assert worker.calls_of("check") == []


async def test_challenge_solve_stores_then_check_path(router, worker, cache):
    worker.solve_results.append(challenge_result(URL))
    out = await router.request(params())
    assert out.message == "Challenge solved!"
    assert cache.get("example.com", "203.0.113.7") is not None

    out2 = await router.request(params())
    assert out2.ok and out2.user_agent == UA
    assert len(worker.calls_of("solve")) == 1
    assert len(worker.calls_of("check")) == 1


async def test_check_challenge_drops_entry_and_solves(router, worker, cache):
    worker.solve_results.append(challenge_result(URL))
    await router.request(params())
    worker.check_results.append(
        CheckResult(snapshot=snapshot(URL, title="Just a moment...", html="<title>Just a moment...</title>"))
    )
    out = await router.request(params())
    assert out.ok
    assert len(worker.calls_of("solve")) == 2
    assert cache.get("example.com", "203.0.113.7") is None


async def test_check_internal_error_solves_and_keeps_entry(router, worker, cache):
    worker.solve_results.append(challenge_result(URL))
    await router.request(params())
    worker.check_results.append(CheckResult(error=WorkerError(type="internal", detail="ua mismatch")))
    await router.request(params())
    assert len(worker.calls_of("solve")) == 2
    assert cache.get("example.com", "203.0.113.7") is not None


async def test_proxy_uses_proxy_key(router, worker, cache):
    worker.solve_results.append(challenge_result(URL))
    proxy = ProxySpec(url="http://proxy:3128")
    await router.request(params(proxy=proxy))
    # stored under the worker-reported egress by the solve result; the lookup key is the proxy
    cache.put("example.com", "proxy:http://proxy:3128", UA, challenge_result(URL).snapshot.cookies)
    await router.request(params(proxy=proxy))
    assert len(worker.calls_of("check")) == 1


async def test_proxy_key_hides_credentials(router, worker, cache):
    worker.solve_results.append(challenge_result(URL))
    proxy = ProxySpec(url="http://u:p@proxy:3128")
    await router.request(params(proxy=proxy))
    cache.put("example.com", "proxy:http://proxy:3128", UA, challenge_result(URL).snapshot.cookies)
    await router.request(params(proxy=proxy))
    assert len(worker.calls_of("check")) == 1


async def test_session_bypasses_cache(router, worker, cache):
    worker.solve_results.append(challenge_result(URL))
    out = await router.request(params(session="s1"))
    assert out.ok
    assert cache.entries() == []
    await router.request(params(session="s1"))
    assert worker.calls_of("check") == []


async def test_worker_timeout(router, worker):
    worker.raise_on_solve = TimeoutError("slow")
    out = await router.request(params(max_timeout_ms=2000))
    assert not out.ok and out.failure == "timeout"
    assert out.message == "Error solving the challenge. Timeout after 2.0 seconds."


async def test_plain_result_not_stored(router, worker, cache):
    worker.solve_results.append(plain_result(URL))
    await router.request(params())
    assert cache.entries() == []


async def test_solve_tracked_during_execution(worker, cache):
    from mendarr.core.tracker import SolveTracker

    tracker = SolveTracker()
    r = Router(Settings(), worker, cache, tracker=tracker)

    seen_in_solve = []
    orig_solve = worker.solve

    async def solve_hook(req):
        seen_in_solve.extend(tracker.items())
        return await orig_solve(req)

    worker.solve = solve_hook
    out = await r.request(params(url="https://tracked.org/page"))
    assert out.ok
    assert len(seen_in_solve) == 1
    assert seen_in_solve[0]["type"] == "solve"
    assert seen_in_solve[0]["domain"] == "tracked.org"
    assert tracker.count() == 0  # Finished and cleaned up
