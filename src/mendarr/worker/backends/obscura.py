"""obscura launcher: headed browser via obscura CDP server on a slot's port."""

import asyncio
import contextlib
import logging
import os
import shutil
from typing import Any

from mendarr.config import Settings
from mendarr.proxy import redact
from mendarr.worker.backends import BackendUnavailable, UpstreamUnreachable
from mendarr.worker.backends.camoufox import LaunchedBrowser

log = logging.getLogger("mendarr.worker.backends.obscura")

_launch_locks: dict[int, asyncio.Lock] = {}

# obscura serve ports: base + slot number (e.g., 9222, 9223, ...)
_OBSCURA_BASE_PORT = 9222


def _get_launch_lock(slot: int) -> asyncio.Lock:
    global _launch_locks
    if slot not in _launch_locks:
        _launch_locks[slot] = asyncio.Lock()
    return _launch_locks[slot]


async def _find_obscura_binary() -> str:
    """Find the obscura binary in PATH or common locations."""
    # Check PATH first
    binary = shutil.which("obscura")
    if binary:
        return binary
    # Check common install locations
    for path in [
        "/usr/local/bin/obscura",
        "/opt/obscura/obscura",
        "/app/obscura",
        os.path.expanduser("~/.local/bin/obscura"),
    ]:
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path
    raise BackendUnavailable(
        "obscura binary not found. Install it from https://github.com/h4ckf0r0day/obscura/releases "
        "or add it to PATH. In Docker, install it in the browser image."
    )


async def launch(display: int, *, settings: Settings, exit_ip: Any = None) -> LaunchedBrowser:
    """
    Launch obscura CDP server and connect to it via Playwright.

    Args:
        display: X display number (used to derive CDP port)
        settings: Worker settings (upstream proxy, geoip, etc.)
        exit_ip: Optional callable to verify upstream proxy exit IP

    Returns:
        LaunchedBrowser with connected Playwright browser, user agent, and close function
    """
    # Obscura doesn't use X displays; we use the display number to pick a unique CDP port
    cdp_port = _OBSCURA_BASE_PORT + display
    cdp_endpoint = f"ws://127.0.0.1:{cdp_port}/devtools/browser"

    binary = await _find_obscura_binary()
    upstream = settings.upstream

    # Build obscura serve command
    cmd = [
        binary,
        "serve",
        "--port",
        str(cdp_port),
        "--stealth",  # Enable anti-detection + tracker blocking
        "--allow-private-network",  # Allow localhost/LAN targets
    ]

    if upstream is not None:
        # obscura accepts --proxy for HTTP/SOCKS5
        cmd.extend(["--proxy", upstream.httpx_url()])

    # Launch obscura subprocess
    safe_cmd = [redact(arg) if arg.startswith(("http://", "https://", "socks5://")) else arg for arg in cmd]
    log.info("Starting obscura on port %d: %s", cdp_port, " ".join(safe_cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,  # never read: a full pipe would block obscura
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "DISPLAY": f":{display}"},  # Keep DISPLAY for any X11 deps
    )

    # Wait for CDP server to be ready
    try:
        await _wait_for_cdp(cdp_endpoint)
    except BaseException:
        await _terminate_process(proc)
        raise

    # Connect via Playwright
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        await _terminate_process(proc)
        raise BackendUnavailable("playwright is not installed in this worker") from e

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(cdp_endpoint)
        page = await browser.new_page()
        try:
            ua = await page.evaluate("navigator.userAgent")
        finally:
            await page.close()
    except BaseException:
        await pw.stop()
        await _terminate_process(proc)
        raise

    async def close() -> None:
        with contextlib.suppress(Exception):
            await browser.close()
        await pw.stop()
        await _terminate_process(proc)

    return LaunchedBrowser(browser, ua, close)


async def _wait_for_cdp(endpoint: str, timeout: float = 30.0) -> None:
    """Wait for obscura CDP endpoint to become available."""
    import httpx

    http_endpoint = endpoint.replace("ws://", "http://").replace("/devtools/browser", "/json/version")
    start = asyncio.get_event_loop().time()
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            try:
                resp = await client.get(http_endpoint)
                if resp.status_code == 200:
                    log.info("obscura CDP ready at %s", endpoint)
                    return
            except Exception:
                pass
            if asyncio.get_event_loop().time() - start > timeout:
                raise BackendUnavailable(f"obscura CDP server did not start within {timeout}s at {endpoint}")
            await asyncio.sleep(0.5)


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Gracefully terminate obscura subprocess."""
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()