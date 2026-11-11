import asyncio

import pytest
from fakeworker import UA, clearance_cookie, snapshot

from mendarr.api.worker_client import WorkerRejected, WorkerUnavailable
from mendarr.core.cookies import CookieCache
from mendarr.core.manual import ManualError, ManualManager, ManualSession
from mendarr.core.traces import TraceStore
from mendarr.plugins.store import PluginStore
from mendarr.rpc import ManualAutoSolved, ManualResult, PluginSpec, ManualStart, ManualStarted, ManualState

URL = "https://example.org/x"
DEMO = {"events": [{"t": "pointerdown"}], "frames": []}


def challenge_snap():
    return snapshot(URL, status=403, title="Just a moment...", headers={"cf-mitigated": "challenge"})


def clear_snap(url=URL):
    return snapshot(url, cookies=[clearance_cookie("example.org")])


def state(snap, seen=False, status="running"):
    return ManualState(id="m1", status=status, challenge_seen=seen, snapshot=snap, events=3)


class FakeManualWorker:
    def __init__(self, states=None):
        self.states = list(states or [])
        self.start_error: Exception | None = None
        self.state_error: Exception | None = None
        self.started: list[ManualStart] = []
        self.polls = 0
        self.finished = 0
        self.cancelled: list[str] = []
        self.result = ManualResult(
            challenge_seen=True, snapshot=clear_snap(), user_agent=UA, egress="203.0.113.7", demo=DEMO
        )

    async def manual_start(self, req):
        if self.start_error:
            raise self.start_error
        self.started.append(req)
        return ManualStarted(id="m1", slot=2, user_agent=UA, egress="203.0.113.7")

    async def manual_state(self, sid):
        if self.state_error:
            raise self.state_error
        self.polls += 1
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]

    async def manual_finish(self, sid):
        self.finished += 1
        return self.result

    async def manual_cancel(self, sid):
        self.cancelled.append(sid)
        return True


@pytest.fixture
def parts(tmp_path):
    store = PluginStore.open(tmp_path / "plugins")
    cache = CookieCache.open(tmp_path / "c.db", max_ttl_s=3600)
    traces = TraceStore.open(tmp_path / "traces")
    yield store, cache, traces
    store.close()
    cache.close()


def incident(store):
    return store.note_failure("example.org", kind="challenge_loop", url=URL, trace_id="", plugin="_default@builtin")


def manager(parts, worker, **kw):
    store, cache, traces = parts
    kw.setdefault("poll_s", 0.01)
    return ManualManager(worker, store, cache, traces, **kw)


async def finished(s, timeout=2.0):
    await asyncio.wait_for(s.task, timeout)
    assert not s.active


async def settle_then_finish(m, s, w):
    """The watcher must not succeed on its own, however many polls pass; then the user finishes."""
    start = w.polls
    for _ in range(300):
        if w.polls >= start + 4:
            break
        await asyncio.sleep(0.01)
    assert s.active and w.finished == 0
    assert await m.finish(s.id) is True


async def test_start_moves_incident_to_manual(parts):
    store = parts[0]
    iid = incident(store)
    w = FakeManualWorker([state(None)])
    m = manager(parts, w, give_up_s=lambda: 100.0)
    s = await m.start(iid)
    try:
        inc = store.incident(iid)
        assert inc["state"] == "manual" and inc["tier"] == 3 and inc["next_attempt_at"] is not None
        assert s.slot == 2 and s.user_agent == UA
        assert m.active_for_slot(2) is s
        assert (await m.start(iid)) is s
        assert len(w.started) == 1
    finally:
        await m.aclose()


async def test_start_errors(parts):
    store = parts[0]
    m = manager(parts, FakeManualWorker([state(None)]))
    with pytest.raises(ManualError) as e:
        await m.start(999)
    assert e.value.status == 404
    iid = incident(store)
    store.update_incident(iid, state="solved")
    with pytest.raises(ManualError) as e:
        await m.start(iid)
    assert e.value.status == 409


async def test_start_worker_errors(parts):
    iid = incident(parts[0])
    w = FakeManualWorker([state(None)])
    m = manager(parts, w)
    w.start_error = WorkerRejected(409, "no slot")
    with pytest.raises(ManualError) as e:
        await m.start(iid)
    assert e.value.status == 409
    w.start_error = WorkerUnavailable("down")
    with pytest.raises(ManualError) as e:
        await m.start(iid)
    assert e.value.status == 503
    assert parts[0].incident(iid)["state"] == "open"


async def test_solve_flow(parts):
    store, cache, traces = parts
    iid = incident(store)
    w = FakeManualWorker([state(challenge_snap(), seen=True), state(clear_snap(), seen=True)])
    m = manager(parts, w)
    s = await m.start(iid)
    await settle_then_finish(m, s, w)
    assert s.status == "solved" and w.finished == 1
    entry = cache.get("example.org", "203.0.113.7")
    assert entry is not None and entry.ua == UA
    assert any(c.name == "cf_clearance" for c in entry.cookies)
    assert store.incident(iid)["state"] == "solved"
    assert [a["result"] for a in store.attempts(iid)] == ["manual_solved"]
    t = traces.latest("example.org", "demo")
    assert t is not None and t.meta["extra"]["demo"] == DEMO
    assert s.trace_id


@pytest.mark.parametrize("snap", [clear_snap("https://other.net/"), clear_snap("about:blank")])
async def test_other_domain_or_blank_is_not_success(parts, snap):
    iid = incident(parts[0])
    w = FakeManualWorker([state(snap, seen=True)])
    m = manager(parts, w, timeout_s=0.1)
    s = await m.start(iid)
    await finished(s)
    assert s.status == "expired" and w.finished == 0


async def test_watcher_never_finishes_on_its_own(parts):
    iid = incident(parts[0])
    w = FakeManualWorker([state(challenge_snap(), seen=True), state(clear_snap(), seen=True)])
    m = manager(parts, w)
    s = await m.start(iid)
    await asyncio.sleep(0.2)
    assert s.active and w.finished == 0
    assert "Finish" in s.detail
    await m.cancel(s.id)


async def test_expiry_keeps_incident_manual(parts):
    store = parts[0]
    iid = incident(store)
    w = FakeManualWorker([state(challenge_snap())])
    m = manager(parts, w, timeout_s=0.05)
    s = await m.start(iid)
    await finished(s)
    assert s.status == "expired" and w.cancelled == ["m1"]
    assert store.incident(iid)["state"] == "manual"
    assert [a["result"] for a in store.attempts(iid)] == ["manual_expired"]


async def test_cancel(parts):
    store = parts[0]
    iid = incident(store)
    m = manager(parts, FakeManualWorker([state(challenge_snap())]))
    s = await m.start(iid)
    assert await m.cancel(s.id) is True
    assert s.status == "cancelled"
    assert await m.cancel(s.id) is False
    assert await m.cancel("nope") is False
    assert [a["result"] for a in store.attempts(iid)] == ["manual_cancelled"]


async def test_tab_closed_cancels(parts):
    iid = incident(parts[0])
    m = manager(parts, FakeManualWorker([state(None, status="closed")]))
    s = await m.start(iid)
    await finished(s)
    assert s.status == "cancelled" and "tab" in s.detail


async def test_worker_lost_session_fails(parts):
    iid = incident(parts[0])
    w = FakeManualWorker([state(None)])
    w.state_error = WorkerRejected(404, "gone")
    m = manager(parts, w)
    s = await m.start(iid)
    await finished(s)
    assert s.status == "failed"
    assert [a["result"] for a in parts[0].attempts(iid)] == ["manual_failed"]


async def test_hold_policy(parts):
    store = parts[0]
    iid = incident(store)
    on = False
    m = manager(parts, FakeManualWorker([state(None)]), hold_policy=lambda d: on)
    assert m.should_hold("example.org") is False
    on = True
    assert m.should_hold("example.org") is False  # incident not in state manual
    store.update_incident(iid, state="manual")
    assert m.should_hold("example.org") is True
    assert m.should_hold("other.net") is False
    assert await m.wait("example.org", 0.05) is False
    assert await m.wait("example.org", 0) is False


async def test_hold_released_by_solve(parts):
    iid = incident(parts[0])
    w = FakeManualWorker([state(challenge_snap(), seen=True), state(clear_snap(), seen=True)])
    m = manager(parts, w, hold_policy=lambda d: True)
    s = await m.start(iid)
    waiter = asyncio.create_task(m.wait("example.org", 2.0))
    await asyncio.sleep(0.05)
    assert await m.finish(s.id) is True
    assert await waiter is True
    assert s.status == "solved"


async def test_start_adhoc_success(parts):
    w = FakeManualWorker([state(challenge_snap(), seen=True), state(clear_snap(), seen=True)])
    m = manager(parts, w)
    s = await m.start_adhoc(URL, backend="patchright")
    assert s.incident_id is None
    assert s.backend == "patchright"
    assert s.domain == "example.org"
    assert w.started[0].backend == "patchright"
    await settle_then_finish(m, s, w)
    assert s.status == "solved"
    cached = parts[1].get("example.org", "203.0.113.7")
    assert cached and cached.cookies


async def test_start_adhoc_invalid_url(parts):
    m = manager(parts, FakeManualWorker([]))
    with pytest.raises(ValueError):
        await m.start_adhoc("not-a-url")
    with pytest.raises(ValueError):
        await m.start_adhoc("ftp://example.org/resource")


async def test_clear_finished(parts):
    m = manager(parts, FakeManualWorker([]))
    s1 = ManualSession(id="s1", domain="a.org", status="running")
    s2 = ManualSession(id="s2", domain="b.org", status="solved")
    s3 = ManualSession(id="s3", domain="c.org", status="failed")
    m._sessions = {"s1": s1, "s2": s2, "s3": s3}
    cleared = m.clear_finished()
    assert cleared == 2
    assert list(m._sessions.keys()) == ["s1"]


async def test_explicit_finish(parts):
    iid = incident(parts[0])
    w = FakeManualWorker([state(clear_snap())])
    m = manager(parts, w)
    s = await m.start(iid)
    # Manual session does not auto-solve without challenge_seen
    await asyncio.sleep(0.05)
    assert s.active and w.finished == 0
    # Explicit user finish succeeds and caches cookies
    assert await m.finish(s.id) is True
    assert s.status == "solved" and w.finished == 1
    cached = parts[1].get("example.org", "203.0.113.7")
    assert cached and cached.cookies


async def test_finish_on_challenge_page_fails_without_caching(parts):
    store = parts[0]
    iid = incident(store)
    w = FakeManualWorker([state(challenge_snap(), seen=True)])
    w.result = ManualResult(challenge_seen=True, snapshot=challenge_snap(), user_agent=UA, egress="203.0.113.7")
    m = manager(parts, w)
    s = await m.start(iid)
    assert await m.finish(s.id) is True
    assert s.status == "failed" and "not cleared" in s.detail
    assert parts[1].get("example.org", "203.0.113.7") is None
    assert store.incident(iid)["state"] == "manual"
    assert w.cancelled == ["m1"]


async def test_concurrent_finish_captures_once(parts):
    iid = incident(parts[0])
    w = FakeManualWorker([state(clear_snap(), seen=True)])
    gate = asyncio.Event()
    orig = w.manual_finish

    async def slow_finish(sid):
        await gate.wait()
        return await orig(sid)

    w.manual_finish = slow_finish
    m = manager(parts, w)
    s = await m.start(iid)
    first = asyncio.create_task(m.finish(s.id))
    await asyncio.sleep(0.05)
    assert s.finishing
    assert await m.finish(s.id) is False
    gate.set()
    assert await first is True
    assert s.status == "solved" and w.finished == 1


async def test_failure_releases_the_worker_session(parts):
    iid = incident(parts[0])
    w = FakeManualWorker([state(None)])
    w.state_error = WorkerRejected(404, "gone")
    m = manager(parts, w)
    s = await m.start(iid)
    await finished(s)
    assert s.status == "failed" and w.cancelled == ["m1"]


@pytest.mark.parametrize("backend", ["nope", "obscura"])
async def test_manual_backend_is_validated(parts, backend):
    iid = incident(parts[0])
    w = FakeManualWorker([state(None)])
    m = manager(parts, w)
    with pytest.raises(ManualError) as e:
        await m.start_adhoc(URL, backend=backend)
    assert e.value.status == 400
    with pytest.raises(ManualError) as e:
        await m.start(iid, backend=backend)
    assert e.value.status == 400
    assert w.started == []


async def test_autosolve_success_keeps_session_open(parts):
    iid = incident(parts[0])
    w = FakeManualWorker([state(challenge_snap(), seen=True)])
    calls = []

    async def manual_autosolve(sid, req):
        calls.append(req)
        return ManualAutoSolved(ok=True, plugin="x@v1")

    w.manual_autosolve = manual_autosolve
    spec = PluginSpec(scope="x", version=1, source="", sha256="")
    m = manager(parts, w, plugin_resolver=lambda url: spec)
    s = await m.start(iid)
    await m.autosolve(s.id)
    assert s.active and w.finished == 0
    assert s.detail == "auto solve: cleared — click Finish & Capture"
    assert calls[0].plugin == spec
    await asyncio.sleep(0.1)  # the watcher keeps the note
    assert s.detail.startswith("auto solve: cleared")
    await m.cancel(s.id)


async def test_autosolve_failure_and_concurrency(parts):
    iid = incident(parts[0])
    w = FakeManualWorker([state(challenge_snap(), seen=True)])
    gate = asyncio.Event()

    async def manual_autosolve(sid, req):
        await gate.wait()
        return ManualAutoSolved(ok=False, kind="challenge", detail="still there")

    w.manual_autosolve = manual_autosolve
    m = manager(parts, w)
    s = await m.start(iid)
    first = asyncio.create_task(m.autosolve(s.id))
    await asyncio.sleep(0.05)
    assert s.autosolving
    with pytest.raises(ManualError) as e:
        await m.autosolve(s.id)
    assert e.value.status == 409
    gate.set()
    await first
    assert s.active and s.detail == "auto solve failed: challenge (still there)"
    with pytest.raises(ManualError) as e:
        await m.autosolve("nope")
    assert e.value.status == 404
    await m.cancel(s.id)
