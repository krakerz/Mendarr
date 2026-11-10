"""patchright launcher: headed Chromium on a slot's X display (the healer's lever against Firefox fingerprint blocks)."""

import asyncio
import contextlib
import os

from mendarr.config import Settings
from mendarr.worker.backends import BackendUnavailable
from mendarr.worker.backends.camoufox import LaunchedBrowser

_launch_lock = asyncio.Lock()


async def launch(display: int, *, settings: Settings) -> LaunchedBrowser:
    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        raise BackendUnavailable("patchright is not installed in this worker") from e

    upstream = settings.upstream
    args = ["--window-size=1280,720"]
    extra: dict = {}
    if upstream is not None:
        extra["proxy"] = upstream.playwright()
        args.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")  # no WebRTC UDP around the proxy

    async with _launch_lock:
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(
                headless=False,
                env={**os.environ, "DISPLAY": f":{display}"},
                args=args,
                **extra,
            )
            page = await browser.new_page()
            try:
                ua = await page.evaluate("navigator.userAgent")
            finally:
                await page.close()
        except BaseException:
            await pw.stop()
            raise

    async def close() -> None:
        with contextlib.suppress(Exception):
            await browser.close()
        await pw.stop()

    return LaunchedBrowser(browser, ua, close)
