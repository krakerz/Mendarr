import asyncio
import hashlib
import importlib.resources

import pytest

from mendarr.config import Settings
from mendarr.rpc import PluginSpec, SolveRequest
from mendarr.worker.egress import Egress
from mendarr.worker.executor import Executor
from mendarr.worker.sessions import SessionManager
from mendarr.worker.slots import SlotPool


def spec(source: str, sha: str | None = None) -> PluginSpec:
    return PluginSpec(
        scope="example.com",
        version=2,
        source=source,
        sha256=sha or hashlib.sha256(source.encode()).hexdigest(),
    )


def builtin_source() -> str:
    return (importlib.resources.files("mendarr.plugins.builtin") / "_default.py").read_text()


def make_executor():
    launched: list = []

    async def launcher(slot):
        launched.append(slot)
        raise AssertionError("must not launch")

    pool = SlotPool(1, launcher)
    ex = Executor(Settings(slots=1, egress_check_url=""), pool, SessionManager(pool), Egress("", 60))
    return ex, launched


def request(plugin: PluginSpec) -> SolveRequest:
    return SolveRequest(url="https://example.com/", timeout_s=10, plugin=plugin)


async def test_guard_rejected_spec():
    ex, launched = make_executor()
    res = await ex.solve(request(spec("import os\n" + builtin_source())))
    assert res.error.type == "guard"
    assert "import os" in res.error.detail
    assert res.plugin == "example.com@v2"
    assert launched == []


async def test_sha_mismatch():
    ex, launched = make_executor()
    res = await ex.solve(request(spec(builtin_source(), sha="0" * 64)))
    assert res.error.type == "guard"
    assert launched == []


async def test_unavailable_backend():
    ex, launched = make_executor()
    src = builtin_source().replace('"camoufox"', '"patchright"', 1)
    res = await ex.solve(request(spec(src)))
    assert res.error.type == "internal"
    assert "patchright" in res.error.detail
    assert launched == []


async def test_upstream_unreachable_is_navigation():
    pytest.importorskip("playwright")
    from mendarr.worker.backends import UpstreamUnreachable

    detail = "Upstream proxy socks5://up:1080 failed: x"

    async def launcher(slot):
        raise UpstreamUnreachable(detail)

    pool = SlotPool(1, launcher)
    ex = Executor(Settings(slots=1, egress_check_url=""), pool, SessionManager(pool), Egress("", 60))
    res = await ex.solve(request(spec(builtin_source())))
    assert res.error.type == "navigation"
    assert res.error.detail == detail


class TwoSlotLauncher:
    async def __call__(self, slot):
        raise AssertionError("must not launch")


async def test_background_solve_without_spare_slot_is_no_slot_at_once():
    pool = SlotPool(2, TwoSlotLauncher())
    ex = Executor(Settings(slots=2, egress_check_url=""), pool, SessionManager(pool), Egress("", 60))
    req = request(spec(builtin_source())).model_copy(update={"background": True})
    async with pool.acquire():  # one of two busy: no spare left
        res = await asyncio.wait_for(ex.solve(req), 1)
    assert res.error.type == "no_slot"


class LaunchAttempted(Exception):
    pass


async def test_background_solve_with_spare_slots_runs():
    launches = []

    async def launcher(slot):
        launches.append(slot)
        raise LaunchAttempted  # past the guard; whether the executor re-raises or reports it doesn't matter

    pool = SlotPool(2, launcher)
    ex = Executor(Settings(slots=2, egress_check_url=""), pool, SessionManager(pool), Egress("", 60))
    req = request(spec(builtin_source())).model_copy(update={"background": True})
    try:
        res = await ex.solve(req)
    except LaunchAttempted:
        res = None
    assert res is None or res.error is None or res.error.type != "no_slot"
    assert launches, "a background solve with two free slots must try to launch a browser"
