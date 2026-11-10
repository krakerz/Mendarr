"""Shared setup for the tier-2 (agent gate / MCP) tests: the healer-loop env plus an AgentGate."""

from types import SimpleNamespace

from fakeworker import FakeWorker
from test_healer_loop import BASE_YAML, Clock, StubLlm, StubNotifier, params, stuck

from mendarr.config import Settings
from mendarr.core.cookies import CookieCache
from mendarr.core.router import Router
from mendarr.core.traces import TraceStore
from mendarr.healer.agent import AgentGate
from mendarr.healer.config import ConfigFile
from mendarr.healer.loop import Healer
from mendarr.plugins.store import PluginStore


def make_env(tmp_path, yaml: str = BASE_YAML, replays_per_hour: int = 30) -> SimpleNamespace:
    clock = Clock()
    worker = FakeWorker()
    store = PluginStore.open(tmp_path, clock=clock)
    cache = CookieCache.open(tmp_path / "c.db", max_ttl_s=3600)
    settings = Settings(data_dir=str(tmp_path), cookie_cache=False)
    router = Router(settings, worker, cache, store=store)
    traces = TraceStore.open(tmp_path / "traces")
    llm, notifier = StubLlm(), StubNotifier()
    cfg_path = tmp_path / "mendarr.yml"
    cfg_path.write_text(yaml)
    config = ConfigFile(cfg_path)
    healer = Healer(
        settings, store, router, traces, config, llm=llm, notifier=notifier, clock=clock, heal_dir=tmp_path / "heal"
    )
    router.hooks = healer
    gate = AgentGate(store, router, traces, config, healer, replays_per_hour=replays_per_hour, clock=clock)

    async def fail(domain="example.com") -> None:
        worker.solve_results.append(stuck(domain))
        await router.request(params(domain))

    def close() -> None:
        cache.close()
        store.close()

    return SimpleNamespace(
        clock=clock, worker=worker, store=store, router=router, traces=traces, llm=llm, notifier=notifier,
        healer=healer, gate=gate, fail=fail, close=close, tmp=tmp_path,
    )
