"""Camoufox launcher: one headed browser on a slot's X display."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mendarr.config import Settings
from mendarr.worker.backends import UpstreamUnreachable

_launch_lock = asyncio.Lock()  # two concurrent Camoufox launches race on the driver

# with an upstream proxy: no silent direct fallback, DNS at the proxy for SOCKS, WebRTC only through the proxy
_PROXY_PREFS = {
    "network.proxy.failover_direct": False,
    "network.proxy.socks_remote_dns": True,
    "media.peerconnection.ice.proxy_only": True,
}


@dataclass
class LaunchedBrowser:
    browser: Any  # playwright Browser
    user_agent: str
    close: Callable[[], Awaitable[None]]


Launcher = Callable[[int], Awaitable[LaunchedBrowser]]


async def launch(
    display: int, *, settings: Settings, exit_ip: Callable[[], Awaitable[str]] | None = None
) -> LaunchedBrowser:
    from camoufox.async_api import AsyncCamoufox

    upstream = settings.upstream
    geoip: bool | str = settings.geoip
    extra: dict[str, Any] = {}
    if upstream is not None:
        # launch level: contexts without a request proxy inherit it, a request proxy still overrides it per context
        extra = {"proxy": upstream.playwright(), "firefox_user_prefs": dict(_PROXY_PREFS)}
        if settings.geoip and exit_ip is not None:
            ip = await exit_ip()
            if ip == "unknown":
                raise UpstreamUnreachable(f"Upstream proxy {upstream} failed: exit IP lookup through it failed")
            if ip != "default":
                geoip = ip  # Camoufox's own lookup uses requests, which resolves DNS locally for socks5
    async with _launch_lock:
        cm = AsyncCamoufox(
            headless=False,
            humanize=True,
            geoip=geoip,
            window=(1280, 720),
            env={**os.environ, "DISPLAY": f":{display}"},  # Camoufox replaces the env when given one
            **extra,
        )
        browser = await cm.__aenter__()
        page = await browser.new_page()
        try:
            ua = await page.evaluate("navigator.userAgent")
        finally:
            await page.close()
    return LaunchedBrowser(browser, ua, lambda: cm.__aexit__(None, None, None))
