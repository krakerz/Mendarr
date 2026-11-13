import pytest
from fakeworker import FakeWorker, UA, challenge_result, clearance_cookie, plain_result, snapshot

from mendarr.config import Settings
from mendarr.core.cookies import CookieCache
from mendarr.core.router import Router, V1Params
from mendarr.rpc import CheckResult, ProxySpec, SolveResult, WorkerError

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


async def test_via_values(router, worker):
    out = await router.request(params())
    assert out.via == "no_challenge" and out.plugin and out.egress
    worker.solve_results.append(challenge_result(URL))
    assert (await router.request(params(url="http://other.org/"))).via == "solved"
    out = await router.request(params(url="http://other.org/"))
    assert out.via == "cache_hit" and out.egress == "203.0.113.7"
    worker.solve_results.append(SolveResult(error=WorkerError(type="navigation", detail="x")))
    assert (await router.request(params(url="http://third.org/"))).via.startswith("failed:")
    worker.raise_on_solve = TimeoutError("slow")
    assert (await router.request(params(url="http://fourth.org/"))).via == "failed:timeout"


async def test_request_log_has_path_not_query(router, worker, caplog):
    caplog.set_level("INFO", logger="mendarr.core.router")
    await router.request(params(url="http://example.com/a/b?token=SECRET123"))
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("request:"))
    assert "domain=example.com" in line and "url=/a/b " in line and "via=no_challenge" in line
    assert "SECRET123" not in line


async def test_touch_sets_url_and_last_used(router, worker, cache):
    worker.solve_results.append(challenge_result(URL))
    await router.request(params())
    e = cache.entries()[0]
    assert e.url == URL
    await router.request(params(url="http://example.com/other"))
    assert cache.entries()[0].url == "http://example.com/other"


async def test_expired_sibling_cookie_not_in_cache_hit(worker, tmp_path):
    from mendarr.rpc import Cookie

    now = [1000.0]
    c = CookieCache.open(tmp_path / "e.db", max_ttl_s=0, clock=lambda: now[0])
    r = Router(Settings(), worker, c)
    short = Cookie(name="short", value="1", domain="example.com", expires=1010.0)
    clear = Cookie(name="cf_clearance", value="t", domain="example.com", expires=5000.0)
    c.put("example.com", "203.0.113.7", UA, [short, clear], url=URL)
    now[0] = 1100.0
    out = await r.request(params())
    assert out.via == "cache_hit"
    assert [x.name for x in out.cookies] == ["cf_clearance"]
    c.close()


async def test_mismatch_counted(worker, cache, tmp_path):
    from mendarr.core.stats import CHECK_MISMATCH, DomainStats

    stats = DomainStats.open(tmp_path / "st.db")
    r = Router(Settings(), worker, cache, stats=stats)
    worker.solve_results.append(challenge_result(URL))
    await r.request(params())
    worker.check_results.append(
        CheckResult(snapshot=snapshot(URL, title="Just a moment...", html="<title>Just a moment...</title>"))
    )
    out = await r.request(params())  # default solve result: plain, no challenge
    assert out.via == "no_challenge"
    assert stats.counters(CHECK_MISMATCH) == {"example.com": 1}
    assert stats.last()["example.com"][0] == "no_challenge"
    stats.close()


def _entry(cache):
    cache.put("example.com", "203.0.113.7", UA, [clearance_cookie()], url=URL)
    return cache.entries()[0]


CHALLENGED = CheckResult(snapshot=snapshot(URL, title="Just a moment...", html="<title>Just a moment...</title>"))


async def test_refresh_clean_ok(router, worker, cache):
    e = _entry(cache)
    assert await router.refresh(e, renew_before=0) == "ok"
    assert worker.calls_of("check")[0].background is True
    assert worker.calls_of("solve") == []


async def test_refresh_check_error_keeps_entry(router, worker, cache):
    e = _entry(cache)
    worker.check_results.append(CheckResult(error=WorkerError(type="internal", detail="x")))
    assert await router.refresh(e, renew_before=0) == "error"
    assert cache.entries()


async def test_refresh_challenged_solved(router, worker, cache):
    e = _entry(cache)
    worker.check_results.append(CHALLENGED)
    worker.solve_results.append(challenge_result(URL))
    assert await router.refresh(e, renew_before=0) == "solved"
    assert worker.calls_of("solve")[0].background is True
    assert len(cache.entries()) == 1


async def test_refresh_challenged_no_slot_keeps_entry(router, worker, cache):
    e = _entry(cache)
    worker.check_results.append(CHALLENGED)
    worker.solve_results.append(SolveResult(error=WorkerError(type="no_slot", detail="busy")))
    assert await router.refresh(e, renew_before=0) == "no_slot"
    assert len(cache.entries()) == 1


async def test_refresh_challenged_no_challenge_drops(worker, cache, tmp_path):
    from mendarr.core.stats import CHECK_MISMATCH, DomainStats

    stats = DomainStats.open(tmp_path / "st.db")
    r = Router(Settings(), worker, cache, stats=stats)
    e = _entry(cache)
    worker.check_results.append(CHALLENGED)
    assert await r.refresh(e, renew_before=0) == "no_challenge"
    assert cache.entries() == []
    assert stats.counters(CHECK_MISMATCH) == {"example.com": 1}
    stats.close()


async def test_refresh_challenged_solve_failed_drops(router, worker, cache):
    e = _entry(cache)
    worker.check_results.append(CHALLENGED)
    worker.solve_results.append(SolveResult(error=WorkerError(type="navigation", detail="x")))
    assert await router.refresh(e, renew_before=0) == "solve_failed"
    assert cache.entries() == []


async def test_refresh_renews_when_expiring(router, worker, cache):
    e = _entry(cache)
    worker.solve_results.append(challenge_result(URL))
    assert await router.refresh(e, renew_before=e.expires_at + 10) == "renewed"
    assert worker.calls_of("solve")[0].background is True


async def test_refresh_renew_without_challenge_is_ok(router, worker, cache):
    e = _entry(cache)
    assert await router.refresh(e, renew_before=e.expires_at + 10) == "ok"
    assert len(cache.entries()) == 1


async def test_refresh_renew_no_slot(router, worker, cache):
    e = _entry(cache)
    worker.no_slot = True
    assert await router.refresh(e, renew_before=e.expires_at + 10) == "error"  # the check is inconclusive too
    worker.no_slot = False
    worker.solve_results.append(SolveResult(error=WorkerError(type="no_slot", detail="busy")))
    assert await router.refresh(e, renew_before=e.expires_at + 10) == "no_slot"


async def test_background_failure_hook_without_record(worker, cache, tmp_path):
    from mendarr.plugins.store import PluginStore, RollbackPolicy

    class Hooks:
        def __init__(self):
            self.calls = []

        def failure(self, report):
            self.calls.append(report)

        def success(self, domain):
            self.calls.append(domain)

        def rollback(self, event):
            self.calls.append(event)

    store = PluginStore.open(tmp_path, policy=RollbackPolicy(min_samples=3, window=5))
    recorded = []
    store.record = lambda *a, **k: recorded.append(a)
    r = Router(Settings(), worker, cache, store=store)
    r.hooks = Hooks()
    e = _entry(cache)
    worker.check_results.append(CHALLENGED)
    worker.solve_results.append(SolveResult(error=WorkerError(type="navigation", detail="x"), challenge_seen=True))
    await r.refresh(e, renew_before=0)
    assert len(r.hooks.calls) == 1 and r.hooks.calls[0].background is True
    assert recorded == []
    r.hooks.calls.clear()  # no_slot: no incident
    e = _entry(cache)
    worker.check_results.append(CHALLENGED)
    worker.solve_results.append(SolveResult(error=WorkerError(type="no_slot", detail="busy")))
    await r.refresh(e, renew_before=0)
    assert r.hooks.calls == []
    store.close()
