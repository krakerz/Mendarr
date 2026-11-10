import pytest
from agentenv import make_env
from test_healer_loop import BAD, GOOD, ok, stuck

from mendarr.healer.agent import AgentError, RateLimited, RateLimiter

URL = "http://example.com/x"


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    yield e
    e.close()


def solves(env) -> int:
    return sum(1 for name, _ in env.worker.calls if name == "solve")


async def test_guard_violation_is_reported_without_replay(env):
    rep = await env.gate.test(BAD, URL, "c1")
    assert not rep.ok and rep.result == "guard"
    assert solves(env) == 0


@pytest.mark.parametrize("url", ["ftp://x", "nope"])
async def test_bad_url(env, url):
    with pytest.raises(AgentError):
        await env.gate.test(GOOD, url, "c1")


async def test_failing_test_saves_trace_but_no_version(env):
    env.worker.solve_results = [stuck()]
    rep = await env.gate.test(GOOD, URL, "c1")
    assert not rep.ok and rep.result == "replay_failed"
    assert rep.trace_id and env.traces.load(rep.trace_id) is not None
    assert env.store.list("example.com") == []


async def test_passing_test(env):
    env.worker.solve_results = [ok()]
    rep = await env.gate.test(GOOD, URL, "c1")
    assert rep.ok and rep.result == "passed"


async def test_submit_rejections(env):
    with pytest.raises(AgentError):
        await env.gate.submit(GOOD, "example.com", "c1")
    with pytest.raises(AgentError):
        await env.gate.submit(GOOD, "_default", "c1")
    with pytest.raises(AgentError):
        await env.gate.submit(GOOD, "Bad Scope", "c1")


async def test_submit_failing(env):
    await env.fail()
    env.worker.solve_results = [stuck()]
    rep = await env.gate.submit(GOOD, "example.com", "c1")
    assert not rep.ok and rep.result == "replay_failed"
    row = env.store.list("example.com")[-1]
    assert row.author == "agent" and row.status == "rejected"
    [inc] = env.store.open_incidents()
    last = env.store.attempts(inc["id"])[-1]
    assert (last["tier"], last["result"], last["profile"], last["model"]) == (2, "replay_failed", "mcp", "c1")


async def test_submit_passing_heals_and_crosses(env):
    await env.fail()
    await env.fail("other.com")
    env.worker.solve_results = [ok(), ok("other.com")]
    rep = await env.gate.submit(GOOD, "example.com", "c1")
    assert rep.ok and rep.result == "passed"
    assert env.store.resolve("example.com").author == "agent"
    incs = {i["domain"]: i for i in env.store.incidents()}
    assert incs["example.com"]["state"] == "solved"
    assert env.store.attempts(incs["example.com"]["id"])[-1]["tier"] == 2
    assert "healed" in env.notifier.events()
    assert incs["other.com"]["state"] == "solved"
    assert env.store.resolve("other.com").author == "agent"
    cross = env.store.attempts(incs["other.com"]["id"])[-1]
    assert cross["tier"] == 2 and cross["result"] == "passed"


def test_rate_limiter_window(env):
    clock = env.clock
    lim = RateLimiter(2, clock)
    lim.take("a")
    lim.take("a")
    with pytest.raises(RateLimited):
        lim.take("a")
    lim.take("b")
    clock.t += 3601
    lim.take("a")
    unlimited = RateLimiter(0, clock)
    for _ in range(100):
        unlimited.take("a")


async def test_gate_rate_limit_counts_replays_only(tmp_path):
    e = make_env(tmp_path, replays_per_hour=1)
    try:
        await e.gate.test(BAD, URL, "c1")  # guard rejection: free
        e.worker.solve_results = [ok()]
        await e.gate.test(GOOD, URL, "c1")
        with pytest.raises(RateLimited):
            await e.gate.test(GOOD, URL, "c1")
    finally:
        e.close()
