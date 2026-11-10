import pytest
from fakeworker import UA, FakeWorker, challenge_result, plain_result, snapshot

from mendarr.config import Settings
from mendarr.core.cookies import CookieCache
from mendarr.core.router import Router, V1Params
from mendarr.plugins.store import PluginStore, RollbackPolicy
from mendarr.rpc import SolveResult, WorkerError

URL = "http://example.com/x"
SRC = 'META = {"backend": "camoufox", "notes": "n"}\n\n\nasync def solve(page, ctx):\n    return None\n'


class Hooks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def failure(self, report) -> None:
        self.calls.append(("failure", report))

    def success(self, domain) -> None:
        self.calls.append(("success", domain))

    def rollback(self, event) -> None:
        self.calls.append(("rollback", event))


def params() -> V1Params:
    return V1Params(
        url=URL, method="GET", post_data=None, max_timeout_ms=60000, cookies=[], proxy=None, session=None,
        session_ttl_minutes=None, return_only_cookies=False, return_screenshot=False, disable_media=False,
        wait_s=0,
    )


def stuck() -> SolveResult:
    return SolveResult(
        challenge_seen=True, clicks=1, user_agent=UA,
        snapshot=snapshot(URL, status=403, title="Just a moment...", headers={"cf-mitigated": "challenge"}),
    )


@pytest.fixture
def worker():
    return FakeWorker()


@pytest.fixture
def store(tmp_path):
    s = PluginStore.open(tmp_path, policy=RollbackPolicy(min_samples=3, window=5))
    yield s
    s.close()


@pytest.fixture
def hooks():
    return Hooks()


@pytest.fixture
def router(worker, store, hooks, tmp_path):
    cache = CookieCache.open(tmp_path / "c.db", max_ttl_s=3600)
    r = Router(Settings(cookie_cache=False), worker, cache, store=store)
    r.hooks = hooks
    yield r
    cache.close()


async def test_solve_requests_trace(router, worker):
    worker.solve_results = [plain_result(URL)]
    await router.request(params())
    assert worker.calls_of("solve")[-1].trace is True


async def test_failure_reported(router, worker, hooks):
    res = stuck()
    worker.solve_results = [res]
    out = await router.request(params())
    assert not out.ok and out.result is res
    [(name, report)] = hooks.calls
    assert name == "failure" and report.kind == "challenge_loop" and report.result is res
    assert report.domain == "example.com" and report.url == URL and not report.session


async def test_success_only_after_challenge(router, worker, hooks):
    worker.solve_results = [challenge_result(URL)]
    await router.request(params())
    assert hooks.calls == [("success", "example.com")]
    hooks.calls.clear()
    worker.solve_results = [plain_result(URL)]
    await router.request(params())
    assert hooks.calls == []


async def test_replay_calls_no_hooks(router, worker, hooks):
    worker.solve_results = [stuck()]
    out = await router.replay(URL, None)
    assert not out.ok and out.result is not None
    worker.solve_results = [challenge_result(URL)]
    await router.replay(URL, None)
    assert hooks.calls == []


async def test_hook_error_does_not_break_request(router, worker, hooks):
    def boom(_):
        raise RuntimeError("hook")

    hooks.failure = boom
    worker.solve_results = [stuck()]
    out = await router.request(params())
    assert not out.ok and out.failure == "challenge_loop"


async def test_rollback_hook(router, worker, store, hooks):
    store.promote("example.com", store.submit("example.com", SRC).version)
    store.promote("example.com", store.submit("example.com", SRC).version)
    for _ in range(3):
        worker.solve_results = [
            SolveResult(challenge_seen=True, error=WorkerError(type="plugin", detail="boom"), user_agent=UA)
        ]
        await router.request(params())
    rollbacks = [e for n, e in hooks.calls if n == "rollback"]
    assert len(rollbacks) == 1 and rollbacks[0].scope == "example.com"
