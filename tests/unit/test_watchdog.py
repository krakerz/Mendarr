import asyncio
import time

import pytest

from mendarr.worker.watchdog import PluginStalled, Watchdog


async def test_busy_loop_is_interrupted():
    wd = Watchdog(0.3, 30, interval=0.05)
    wd.start()
    t0 = time.monotonic()
    try:
        with pytest.raises(PluginStalled):
            with wd.plugin_running(time.monotonic() + 60):
                t = time.monotonic()
                while time.monotonic() - t < 5:
                    pass
        assert time.monotonic() - t0 < 3
    finally:
        await wd.stop()


async def test_exits_past_deadline():
    codes: list[int] = []
    wd = Watchdog(30, 0.2, exit_fn=codes.append, interval=0.05)
    wd.start()
    try:
        with wd.plugin_running(time.monotonic() - 1):
            await asyncio.sleep(0.6)
    finally:
        await wd.stop()
    assert codes == [70]


async def test_idle_does_nothing():
    codes: list[int] = []
    wd = Watchdog(0.1, 0.2, exit_fn=codes.append, interval=0.05)
    wd.start()
    try:
        await asyncio.sleep(0.3)
    finally:
        await wd.stop()
    assert codes == []
