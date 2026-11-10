"""Plugin-facing API: the Ctx a plugin receives next to the Playwright page."""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mendarr.core import markers
from mendarr.sdk import human, turnstile

if TYPE_CHECKING:
    from playwright.async_api import Frame, Locator, Page
else:
    Page = Frame = Locator = Any

__all__ = ["Ctx", "Page", "Frame"]

POLL = 0.25
CLEAR_POLLS = 2  # consecutive clear polls that count as "gone"


class Ctx:
    """What a plugin gets besides the page. All waits are capped by the request deadline."""

    def __init__(
        self,
        page: Page,
        *,
        deadline: float,
        display: int | None = None,
        click_backend: str = "auto",
        main_headers: Callable[[], Awaitable[dict[str, str]]] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.page = page
        self.deadline = deadline  # time.monotonic() value
        self.display = display
        self.click_backend = click_backend
        self.clicks = 0  # successful human_click calls
        self.steps: list[str] = []
        self.screenshots: list[bytes] = []
        self._main_headers = main_headers
        self._logger = logger or logging.getLogger("mendarr.plugin")

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def log(self, msg: str) -> None:
        self.steps.append(msg)
        self._logger.info("%s", msg)

    async def challenge_present(self) -> bool:
        # No page.evaluate: running scripts on a challenge page can reset it
        try:
            if (await self.page.title()).strip().lower() in markers.CHALLENGE_TITLES:
                return True
            if await self.page.locator(markers.CHALLENGE_CSS).count() > 0:
                return True
            if self._main_headers is not None:
                return (await self._main_headers()).get("cf-mitigated") == "challenge"
        except Exception:
            return True  # navigation in flight or context destroyed: not known clear
        return False

    async def wait_challenge(self, timeout: float = 15) -> bool:
        end = time.monotonic() + min(timeout, self.remaining())
        clear = 0
        while time.monotonic() < end:
            if await self.challenge_present():
                return True
            clear += 1
            if clear >= CLEAR_POLLS:
                return False
            await asyncio.sleep(POLL)
        return await self.challenge_present()

    async def wait_clearance(self, timeout: float = 20) -> bool:
        end = time.monotonic() + min(timeout, self.remaining())
        clear = 0
        while time.monotonic() < end:
            if await self.challenge_present():
                clear = 0
            else:
                clear += 1
                if clear >= CLEAR_POLLS:
                    return True
            await asyncio.sleep(POLL)
        return False

    async def find_turnstile_frame(self, timeout: float = 10) -> Frame | None:
        end = time.monotonic() + min(timeout, self.remaining())
        while True:
            for frame in self.page.frames:
                if frame != self.page.main_frame and turnstile.frame_matches(frame.url):
                    return frame
            if time.monotonic() >= end:
                return None
            await asyncio.sleep(POLL)

    async def turnstile_token(self) -> str | None:
        try:
            loc = self.page.locator(markers.TURNSTILE_RESPONSE_INPUT)
            if await loc.count() > 0:
                return await loc.first.input_value(timeout=500) or None
        except Exception:
            pass
        return None

    async def human_click(
        self,
        target: "Frame | Locator | tuple[float, float] | None",
        selector: str | None = None,
    ) -> bool:
        point = await turnstile.click_point(self.page, target, selector)
        if point is None:
            return False
        x, y = point
        clicked, how = await human.click(
            self.page, x, y, display=self.display, backend=self.click_backend
        )
        if clicked:
            self.clicks += 1
            self.log(f"click at {x:.0f},{y:.0f} via {how}")
        return clicked

    async def human_delay(self, lo: float = 0.3, hi: float = 1.2) -> None:
        await asyncio.sleep(min(random.uniform(lo, hi), self.remaining()))

    async def screenshot(self, label: str = "") -> bytes:
        data = await self.page.screenshot(type="png")
        self.screenshots.append(data)
        if label:
            self.log(f"screenshot: {label}")
        return data
