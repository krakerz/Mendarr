import asyncio

from mendarr.config import Settings
from mendarr.rpc import CheckRequest
from mendarr.worker.check import CookieChecker
from mendarr.worker.slots import SlotPool


async def no_launch(slot):
    raise AssertionError("must not launch")


def browser_checker(slots):
    pool = SlotPool(slots, no_launch)
    checker = CookieChecker(Settings(slots=slots, cookie_check="browser"), pool)
    return checker, pool


def req(**kw):
    return CheckRequest(url="https://example.com/?secret=1", cookies=[], user_agent="UA", **kw)


async def test_background_browser_check_without_spare_slot_is_no_slot():
    checker, pool = browser_checker(2)
    async with pool.acquire():
        res = await asyncio.wait_for(checker.check(req(background=True)), 1)
    assert res.error.type == "no_slot"
