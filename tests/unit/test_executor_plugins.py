import asyncio
import hashlib
import importlib.resources

import pytest

from mendarr.config import Settings
from mendarr.rpc import PluginSpec, SolveRequest, SolveResult
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


def with_backend(backend: str) -> str:
    return builtin_source().replace("META = {", f'META = {{"backend": "{backend}", ', 1)


def make_executor():
    launched: list = []

    async def launcher(slot, locale=None):
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
    src = with_backend("patchright")
    res = await ex.solve(request(spec(src)))
    assert res.error.type == "internal"
    assert "patchright" in res.error.detail
    assert launched == []


async def test_upstream_unreachable_is_navigation():
    pytest.importorskip("playwright")
    from mendarr.worker.backends import UpstreamUnreachable

    detail = "Upstream proxy socks5://up:1080 failed: x"

    async def launcher(slot, locale=None):
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
    async def launcher(slot, locale=None):
        raise LaunchAttempted

    pool = SlotPool(2, launcher)
    assert pool.background_ok()
    ex = Executor(Settings(slots=2, egress_check_url=""), pool, SessionManager(pool), Egress("", 60))
    req = request(spec(builtin_source())).model_copy(update={"background": True})
    # past the guard the solve fails somewhere later (launch, or backend import without the browser extra in CI);
    # all that matters here is that the guard let it through
    try:
        res = await ex.solve(req)
    except LaunchAttempted:
        return
    assert res.error is None or res.error.type != "no_slot", res.error


async def test_backend_resolution():
    ex, _ = make_executor()
    seen: list[str] = []

    async def fake_run(req, slot, session, deadline, egress, plugin, backend):
        seen.append(backend)
        return SolveResult()

    ex._run = fake_run
    for plugin_src, default, want in [
        (with_backend("obscura"), "patchright", "obscura"),  # explicit META wins
        (builtin_source(), "patchright", "patchright"),  # absent -> the request's default
        (builtin_source(), None, "camoufox"),  # absent + None -> camoufox
        (builtin_source(), "chrome", "camoufox"),  # unknown -> camoufox
    ]:
        seen.clear()
        req = SolveRequest(url="https://example.com/", timeout_s=10, plugin=spec(plugin_src), default_backend=default)
        await ex.solve(req)
        assert seen == [want]
