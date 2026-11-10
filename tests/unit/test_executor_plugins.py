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
