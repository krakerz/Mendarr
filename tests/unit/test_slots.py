import asyncio

import pytest

from mendarr.worker.backends.camoufox import LaunchedBrowser
from mendarr.worker.slots import SlotPool


class FakeBrowser:
    def __init__(self):
        self.connected = True

    def is_connected(self):
        return self.connected


def make_pool(count=1):
    launched: list[tuple[int, FakeBrowser, list]] = []

    async def launcher(slot):
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
