import time
from types import SimpleNamespace

import pytest
from fakeworker import UA, FakeWorker, clearance_cookie, snapshot

from mendarr.config import Settings
from mendarr.core.cookies import CookieCache
from mendarr.core.router import Router, V1Params
from mendarr.core.traces import TraceStore
from mendarr.healer.config import ConfigFile
from mendarr.healer.llm import Endpoint, LlmUnavailable
from mendarr.healer.loop import Healer, _next_midnight
from mendarr.plugins.store import PluginStore
from mendarr.rpc import SolveResult

GOOD = 'META = {"backend": "camoufox", "notes": "fixed"}\n\n\nasync def solve(page, ctx):\n    return None\n'
BAD = "import os\n" + GOOD
GOOD_REPLY = f"```python\n{GOOD}```"
BAD_REPLY = f"```python\n{BAD}```"

BASE_YAML = """
healer: {trace_min_interval_s: 0, llm_wait_s: 60}
escalation:
  auto_attempts: 0
  heal_rounds: 2
  heal_windows: [5m, 30m]
  agent_wait: 1h
  manual: {notify: [discord]}
llm:
  order: [fake]
  profiles: {fake: {base_url: 'http://llm.test/v1', model: fm}}
"""


class Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t


class StubLlm:
    def __init__(self) -> None:
        self.up = True
        self.texts: list = []
        self.calls = 0

    async def pick(self, order, profiles):
        if not self.up or not order:
            return None
        name = order[0]
        return Endpoint(name, profiles[name], "fm-model")

    async def complete(self, ep, messages):
        self.calls += 1
        item = self.texts.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item, elapsed_s=0.5, usage={})

    async def aclose(self):
        pass


class StubNotifier:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, cfg, notice, channels=("discord", "webhook")):
        self.sent.append((notice, list(channels)))
        return {}

    def events(self) -> list[str]:
        return [n.event for n, _ in self.sent]

    async def aclose(self):
        pass


def url_of(domain: str) -> str:
    return f"http://{domain}/x"


def stuck(domain="example.com") -> SolveResult:
    return SolveResult(
        challenge_seen=True, clicks=1, user_agent=UA,
        snapshot=snapshot(url_of(domain), status=403, title="Just a moment...", headers={"cf-mitigated": "challenge"}),
    )


def ok(domain="example.com") -> SolveResult:
    return SolveResult(
        challenge_seen=True, clicks=1, user_agent=UA, egress="203.0.113.7",
        snapshot=snapshot(url_of(domain), cookies=[clearance_cookie(domain)]),
    )


def denied(domain="example.com") -> SolveResult:
    return SolveResult(
        user_agent=UA,
        snapshot=snapshot(
            url_of(domain), status=403, title="Attention Required! | Cloudflare",
            html='<title>Attention Required! | Cloudflare</title><span class="cf-error-code">1020</span>',
        ),
    )


def params(domain="example.com") -> V1Params:
    return V1Params(
        url=url_of(domain), method="GET", post_data=None, max_timeout_ms=60000, cookies=[], proxy=None,
        session=None, session_ttl_minutes=None, return_only_cookies=False, return_screenshot=False,
        disable_media=False, wait_s=0,
    )


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    worker = FakeWorker()
    store = PluginStore.open(tmp_path, clock=clock)
    cache = CookieCache.open(tmp_path / "c.db", max_ttl_s=3600)
    settings = Settings(data_dir=str(tmp_path), cookie_cache=False)
    router = Router(settings, worker, cache, store=store)
    traces = TraceStore.open(tmp_path / "traces")
    llm, notifier = StubLlm(), StubNotifier()
    cfg_path = tmp_path / "mendarr.yml"

    def make(extra: str = "", base: str = BASE_YAML) -> Healer:
        cfg_path.write_text(base + extra)
        healer = Healer(
            settings, store, router, traces, ConfigFile(cfg_path), llm=llm, notifier=notifier, clock=clock,
            heal_dir=tmp_path / "heal",
        )
        router.hooks = healer
        return healer

    async def fail(domain="example.com", result=None) -> None:
        worker.solve_results.append(result or stuck(domain))
        await router.request(params(domain))

    yield SimpleNamespace(
        clock=clock, worker=worker, store=store, router=router, traces=traces, llm=llm, notifier=notifier,
        make=make, fail=fail, tmp=tmp_path,
    )
    cache.close()
    store.close()


def only_incident(env, domain="example.com") -> dict:
    [inc] = [i for i in env.store.incidents() if i["domain"] == domain]
    return inc


async def test_failure_opens_incident_with_trace(env):
    env.make()
    await env.fail()
    inc = only_incident(env)
    assert inc["state"] == "open" and inc["kind"] == "challenge_loop" and inc["url"] == url_of("example.com")
    assert env.traces.load(inc["trace_id"]) is not None


async def test_blocked_and_disabled_open_no_incident(env):
    env.make("domains:\n  off.org: {enabled: false}\n")
    await env.fail(result=denied())
    assert env.store.incidents() == []
    assert env.traces.latest("example.com", "failure") is not None
    await env.fail("off.org")
    assert env.store.incidents() == []
    assert env.traces.latest("off.org", "failure") is not None


async def test_llm_down_waits_without_attempts(env):
    healer = env.make()
    await env.fail()
    env.llm.up = False
    assert await healer.tick() is True
    inc = only_incident(env)
    assert inc["state"] == "open" and inc["next_attempt_at"] == env.clock.t + 60 and inc["attempts"] == 0
    assert await healer.tick() is False


async def test_guard_then_good_candidate_promoted(env):
    healer = env.make()
    await env.fail()
    env.llm.texts = [BAD_REPLY, GOOD_REPLY]
    env.worker.solve_results = [ok()]
    await healer.tick()
    inc = only_incident(env)
    assert inc["state"] == "solved"
    assert [a["result"] for a in env.store.attempts(inc["id"])] == ["guard", "passed"]
    active = env.store.active("example.com")
    assert active is not None and active.author == "llm" and active.status == "active"
    assert env.notifier.events() == ["healed"]
    assert (env.tmp / "heal" / str(inc["id"]) / "a1-prompt.json").exists()
    assert (env.tmp / "heal" / str(inc["id"]) / "a2-response.md").exists()
    assert env.store.attempts(inc["id"])[0]["prompt_ref"] == f"heal/{inc['id']}/a1-prompt.json"


async def test_windows_then_manual_then_gave_up(env):
    healer = env.make()
    await env.fail()
    env.llm.texts = [GOOD_REPLY] * 6
    env.worker.solve_results = [stuck()] * 6
    await healer.tick()
    inc = only_incident(env)
    assert inc["state"] == "open" and inc["heal_window"] == 1 and inc["next_attempt_at"] == env.clock.t + 300
    rows = env.store.attempts(inc["id"])
    assert [a["result"] for a in rows] == ["replay_failed"] * 2 and all(a["trace_id"] for a in rows)
    assert {r.status for r in env.store.list("example.com")} == {"rejected"}
    assert await healer.tick() is False
    env.clock.t += 300
    await healer.tick()
    inc = only_incident(env)
    assert inc["heal_window"] == 2 and inc["next_attempt_at"] == env.clock.t + 1800
    env.clock.t += 1800
    await healer.tick()
    inc = only_incident(env)
    assert inc["state"] == "manual" and inc["tier"] == 3
    notice, channels = env.notifier.sent[-1]
    assert notice.event == "manual" and channels == ["discord"]
    env.clock.t += 7 * 86400
    await healer.tick()
    assert only_incident(env)["state"] == "gave_up"
    assert env.notifier.events()[-1] == "gave_up"


async def test_agent_tier_escalates_then_manual(env):
    base = BASE_YAML.replace("heal_windows: [5m, 30m]", "heal_windows: []").replace(
        "agent_wait: 1h", "agent_wait: 1h\n  agent_tier: on"
    )
    healer = env.make(base=base)
    await env.fail()
    env.llm.texts = [GOOD_REPLY] * 2
    env.worker.solve_results = [stuck()] * 2
    await healer.tick()
    inc = only_incident(env)
    assert inc["state"] == "escalated" and inc["tier"] == 2 and inc["next_attempt_at"] == env.clock.t + 3600
    assert env.notifier.events() == ["agent_wanted"]
    env.clock.t += 3600
    await healer.tick()
    assert only_incident(env)["state"] == "manual"
    assert env.notifier.events()[-1] == "manual"


async def test_daily_cap_defers_to_midnight(env):
    healer = env.make(
        base=BASE_YAML.replace("llm_wait_s: 60}", "llm_wait_s: 60, daily_rounds: 1}")
    )
    await env.fail()
    env.llm.texts = [GOOD_REPLY, GOOD_REPLY]
    env.worker.solve_results = [stuck(), stuck()]
    await healer.tick()
    inc = only_incident(env)
    assert env.llm.calls == 1
    assert inc["state"] == "open" and inc["next_attempt_at"] == _next_midnight(env.clock.t)
    assert "daily" in inc["note"]


async def test_llm_error_midround_waits(env):
    healer = env.make()
    await env.fail()
    env.llm.texts = [LlmUnavailable("fake: down")]
    await healer.tick()
    inc = only_incident(env)
    assert [a["result"] for a in env.store.attempts(inc["id"])] == ["llm_error"]
    assert inc["state"] == "open" and inc["next_attempt_at"] == env.clock.t + 60
    day = time.strftime("%Y-%m-%d", time.localtime(env.clock.t))
    assert env.store.day_rounds("example.com", day) == 0


async def test_tier0_retry_passes_as_transient(env):
    healer = env.make(base=BASE_YAML.replace("auto_attempts: 0", "auto_attempts: 1"))
    await env.fail()
    env.worker.solve_results = [ok()]
    await healer.tick()
    inc = only_incident(env)
    assert inc["state"] == "solved" and inc["note"].startswith("transient")
    assert env.llm.calls == 0
    assert [a["result"] for a in env.store.attempts(inc["id"])] == ["retry_passed"]


async def test_tier0_backend_switch_promoted(env):
    healer = env.make(base=BASE_YAML.replace("auto_attempts: 0", "auto_attempts: 2"))
    await env.fail()
    env.worker.solve_results = [stuck(), ok()]
    await healer.tick()
    inc = only_incident(env)
    assert inc["state"] == "solved"
    assert [a["result"] for a in env.store.attempts(inc["id"])] == ["retry_failed", "switch_passed"]
    active = env.store.active("example.com")
    assert active is not None and active.author == "llm" and active.backend == "patchright"
    assert env.notifier.events() == ["healed"]


async def test_cross_incident_replay_and_default_proposal(env):
    healer = env.make()
    await env.fail("a.org")
    await env.fail("b.org")
    env.llm.texts = [GOOD_REPLY]
    env.worker.solve_results = [ok("a.org"), ok("b.org")]
    await healer.tick()
    assert only_incident(env, "a.org")["state"] == "solved"
    assert only_incident(env, "b.org")["state"] == "solved"
    b = env.store.active("b.org")
    assert b is not None and b.author == "llm"
    [prop] = [r for r in env.store.list("_default") if r.author == "llm"]
    assert prop.status == "candidate" and prop.notes == "proposed _default: passed a.org, b.org"
    assert "default_proposed" in env.notifier.events()
    # the same source is not proposed twice
    await env.fail("c.org")
    env.worker.solve_results = [ok("c.org")]
    rec = env.store.active("a.org")
    await healer._cross(rec, healer._config.get())
    assert len([r for r in env.store.list("_default") if r.author == "llm"]) == 1
    assert only_incident(env, "c.org")["state"] == "solved"


async def test_router_success_recovers_open_incident(env):
    env.make()
    await env.fail()
    assert only_incident(env)["state"] == "open"
    env.worker.solve_results = [ok()]
    out = await env.router.request(params())
    assert out.ok
    inc = only_incident(env)
    assert inc["state"] == "solved" and inc["note"].startswith("recovered")


async def test_healer_tracking_during_work(env):
    from mendarr.core.tracker import SolveTracker

    tracker = SolveTracker()
    cfg_path = env.tmp / "mendarr.yml"
    cfg_path.write_text(BASE_YAML.replace("auto_attempts: 0", "auto_attempts: 1"))
    settings = Settings(data_dir=str(env.tmp), cookie_cache=False)
    healer = Healer(
        settings, env.store, env.router, env.traces, ConfigFile(cfg_path),
        llm=env.llm, notifier=env.notifier, clock=env.clock,
        heal_dir=env.tmp / "heal", tracker=tracker,
    )
    env.router.hooks = healer
    await env.fail()
    assert only_incident(env)["state"] == "open"

    seen_in_replay = []
    orig_replay = env.router.replay

    async def replay_hook(*args, **kwargs):
        seen_in_replay.extend(tracker.items())
        return await orig_replay(*args, **kwargs)

    env.router.replay = replay_hook
    env.worker.solve_results = [ok()]
    await healer.tick()

    assert len(seen_in_replay) >= 1
    assert seen_in_replay[0]["type"] == "healer"
    assert seen_in_replay[0]["domain"] == "example.com"
    assert "auto attempt" in seen_in_replay[0]["description"]
    assert tracker.count() == 0  # Finished and cleaned up
