import asyncio

import pytest

from mendarr.proxy import parse_upstream
from mendarr.rpc import ProxySpec
from mendarr.worker.backends.camoufox import LaunchedBrowser
from mendarr.worker.slots import SlotPool


class FakeBrowser:
    def __init__(self):
        self.connected = True

    def is_connected(self):
        return self.connected


def make_pool(count=1):
    launched: list[tuple[int, FakeBrowser, list]] = []

    async def launcher(slot, locale=None):
        b = FakeBrowser()
        closed: list = []

        async def close():
            closed.append(1)

        launched.append((slot, b, closed))
        return LaunchedBrowser(b, f"UA-{slot}", close)

    return SlotPool(count, launcher), launched


async def test_acquire_serializes_on_one_slot():
    pool, _ = make_pool(1)
    order = []

    async def worker(name):
        async with pool.acquire():
            order.append(f"{name}-in")
            await asyncio.sleep(0.05)
            order.append(f"{name}-out")

    await asyncio.gather(worker("a"), worker("b"))
    assert order[0].endswith("-in") and order[1] == order[0].replace("-in", "-out")


async def test_pinned_acquire_waits_only_for_its_slot():
    pool, _ = make_pool(2)
    async with pool.acquire(slot=1):
        async with asyncio.timeout(0.5):
            async with pool.acquire(slot=2) as s:
                assert s == 2
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                async with pool.acquire(slot=1):
                    pass


async def test_timeout_leaves_pool_consistent():
    pool, _ = make_pool(1)
    async with pool.acquire():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                async with pool.acquire():
                    pass
    async with asyncio.timeout(0.5):
        async with pool.acquire() as s:
            assert s == 1


async def test_lowest_free_slot_first():
    pool, _ = make_pool(3)
    async with pool.acquire() as a, pool.acquire() as b:
        assert (a, b) == (1, 2)


async def test_lazy_launch_and_reuse():
    pool, launched = make_pool(2)
    assert pool.user_agent(1) == ""
    b1 = await pool.browser(1)
    assert await pool.browser(1) is b1
    assert len(launched) == 1
    assert pool.user_agent(1) == "UA-1"


async def test_relaunch_after_disconnect():
    pool, launched = make_pool(1)
    first = await pool.browser(1)
    first.connected = False
    second = await pool.browser(1)
    assert second is not first
    assert len(launched) == 2
    assert launched[0][2] == [1]  # old browser closed


async def test_close_closes_browsers():
    pool, launched = make_pool(1)
    await pool.browser(1)
    await pool.close()
    assert launched[0][2] == [1]


async def test_has_free_with_all_slots_held():
    pool, _ = make_pool(2)
    assert pool.has_free()
    async with pool.acquire(), pool.acquire():
        assert not pool.has_free()
    assert pool.has_free()


async def test_background_ok_keeps_one_slot_spare():
    pool, _ = make_pool(3)
    assert pool.background_ok()
    async with pool.acquire():
        assert pool.background_ok()  # 2 free
        async with pool.acquire():
            assert not pool.background_ok()  # 1 free: kept for real requests


async def test_background_ok_single_slot():
    pool, _ = make_pool(1)
    assert pool.background_ok()
    async with pool.acquire():
        assert not pool.background_ok()


async def test_locale_relaunch_counts_default_contexts():
    browsers: list[FakeBrowser] = []

    async def launcher(slot, locale=None):
        b = FakeBrowser()
        b.contexts = [object()]  # a CDP connection's default context
        browsers.append(b)

        async def close():
            pass

        return LaunchedBrowser(b, "UA", close, locale, base_contexts=1)

    pool = SlotPool(1, launcher)
    await pool.browser(1, "camoufox", "en-US")
    await pool.browser(1, "camoufox", "fr-FR")
    assert len(browsers) == 2  # idle (only the default context): relaunched
    browsers[1].contexts.append(object())  # a real session
    await pool.browser(1, "camoufox", "de-DE")
    assert len(browsers) == 2  # busy: deferred


class CtxBrowser(FakeBrowser):
    def __init__(self):
        super().__init__()
        self.contexts: list = []


def make_proxy_pool(upstream=None):
    """Launchers that take `proxy=`; `calls` = (backend, proxy url or None), `closed` = the closed proxy urls."""
    calls: list[tuple[str, str | None]] = []
    closed: list[str | None] = []

    def launcher(backend):
        async def launch(slot, locale=None, proxy=None):
            url = proxy.url if proxy else None
            calls.append((backend, url))

            async def close():
                closed.append(url)

            return LaunchedBrowser(CtxBrowser(), f"UA-{backend}-{url}", close)

        return launch

    pool = SlotPool(
        1, {"obscura": launcher("obscura"), "camoufox": launcher("camoufox")}, per_proxy={"obscura"}, upstream=upstream
    )
    return pool, calls, closed


A = ProxySpec(url="http://a:1")
B = ProxySpec(url="http://b:2", username="u", password="secret")


async def test_per_proxy_backend_runs_each_proxy_on_its_own_process():
    pool, calls, closed = make_proxy_pool()
    b1 = await pool.browser(1, "obscura", proxy=A)
    assert await pool.browser(1, "obscura", proxy=A) is b1
    assert calls == [("obscura", "http://a:1")]
    await pool.browser(1, "obscura", proxy=B)
    assert closed == ["http://a:1"]  # one per-proxy process per slot: A's closed first
    await pool.browser(1, "obscura")
    assert calls[-1] == ("obscura", None) and closed == ["http://a:1"]  # default left B alone
    assert pool.user_agent(1, "obscura", B) == "UA-obscura-http://b:2"
    assert pool.user_agent(1, "obscura") == "UA-obscura-None"


async def test_other_backends_keep_the_default_process_with_a_proxy():
    pool, calls, _ = make_proxy_pool()
    b = await pool.browser(1, "camoufox", proxy=A)
    assert calls == [("camoufox", None)]
    assert await pool.browser(1, "camoufox") is b


async def test_request_proxy_equal_to_the_upstream_uses_the_default_process():
    pool, calls, _ = make_proxy_pool(upstream=parse_upstream("http://u:p@h:3128"))
    same = ProxySpec(url="http://h:3128", username="u", password="p")
    b = await pool.browser(1, "obscura", proxy=same)
    assert await pool.browser(1, "obscura") is b
    assert calls == [("obscura", None)]


async def test_retire_relaunches_an_idle_browser_but_not_one_with_contexts():
    pool, calls, closed = make_proxy_pool()
    b = await pool.browser(1, "obscura")
    pool.retire("obscura")
    b2 = await pool.browser(1, "obscura")
    assert b2 is not b and len(calls) == 2 and closed == [None]
    b2.contexts.append(object())  # a context in use
    pool.retire("obscura")
    assert await pool.browser(1, "obscura") is b2 and len(calls) == 2
