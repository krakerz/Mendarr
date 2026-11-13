"""Cookie reuse check: replay stored cookies and report what the origin answers."""

import asyncio
import contextlib
import logging
import re
from collections.abc import Iterable
from typing import Literal

from mendarr.config import Settings
from mendarr.core.markers import extract_title
from mendarr.proxy import curl_proxy_url, describe_failure
from mendarr.rpc import CheckRequest, CheckResult, Cookie, ProxySpec, Snapshot, WorkerError
from mendarr.worker.backends import UpstreamUnreachable
from mendarr.worker.pagestate import MainResponseTracker, pw_proxy, snapshot
from mendarr.worker.slots import SlotPool

log = logging.getLogger("mendarr.worker.check")

_SUFFIX_RE = re.compile(r"(\d+)$")


def pick_impersonation(wanted: str, available: Iterable[str]) -> str | None:
    targets = list(available)
    if wanted in targets:
        return wanted
    if wanted == "firefox":
        versions = [(int(m.group(1)), t) for t in targets if t.startswith("firefox") and (m := _SUFFIX_RE.search(t))]
        if versions:
            return max(versions)[1]
    return None


def _available_targets() -> list[str]:
    from curl_cffi.requests import BrowserType

    return [b.value for b in BrowserType]


def _proxy_url(proxy: ProxySpec | None) -> str | None:
    return None if proxy is None else curl_proxy_url(proxy)


class CookieChecker:
    mode: Literal["curl", "browser"]
    target: str | None

    def __init__(self, settings: Settings, pool: SlotPool) -> None:
        self._pool = pool
        upstream = settings.upstream
        # curl needs the default proxy explicitly; the browsers have it from launch
        self._upstream = upstream.spec() if upstream else None
        self.mode, self.target = "browser", None
        if settings.cookie_check == "curl":
            try:
                target = pick_impersonation(settings.curl_impersonate, _available_targets())
            except ImportError:
                target = None
            if target is not None:
                self.mode, self.target = "curl", target
            else:
                log.warning("curl_cffi or a %r target is missing, cookie check uses the browser", settings.curl_impersonate)

    async def check(self, req: CheckRequest) -> CheckResult:
        try:
            if self.mode == "curl":
                return await self._check_curl(req)
            return await self._check_browser(req)
        except TimeoutError:
            return CheckResult(error=WorkerError(type="deadline", detail="Timeout"), mode=self.mode)
        except UpstreamUnreachable as e:
            return CheckResult(error=WorkerError(type="navigation", detail=str(e)), mode=self.mode)

    async def _check_curl(self, req: CheckRequest) -> CheckResult:
        from curl_cffi.requests import AsyncSession

        headers = {"User-Agent": req.user_agent}
        if req.cookies:
            # an explicit header, not curl's jar: the jar silently drops cookies for single-label hosts
            headers["Cookie"] = "; ".join(f"{c.name}={c.value}" for c in req.cookies)
        if req.method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            async with AsyncSession(impersonate=self.target) as s:
                r = await s.request(
                    req.method,
                    req.url,
                    data=req.post_data,
                    headers=headers,
                    proxy=_proxy_url(req.proxy or self._upstream),
                    timeout=req.timeout_s,
                    allow_redirects=True,
                )
                text = r.text
                jar = [
                    Cookie(
                        name=c.name,
                        value=c.value or "",
                        domain=c.domain,
                        path=c.path,
                        expires=c.expires if c.expires is not None else -1,
                        secure=bool(c.secure),
                    )
                    for c in r.cookies.jar
                ]
        except Exception as e:
            detail = describe_failure(str(e), req.proxy or self._upstream)
            log.debug("check: %s %s -> %s", req.method, req.url.split("?")[0], type(e).__name__)
            return CheckResult(error=WorkerError(type="navigation", detail=detail), mode="curl")
        log.debug("check: %s %s -> %s", req.method, req.url.split("?")[0], r.status_code)
        merged = {c.name: c for c in req.cookies}
        merged.update({c.name: c for c in jar})
        snap = Snapshot(
            url=str(r.url),
            status=r.status_code,
            headers={k.lower(): v for k, v in r.headers.items()},
            title=extract_title(text),
            html=text,
            cookies=list(merged.values()),
        )
        return CheckResult(snapshot=snap, mode="curl")

    async def _check_browser(self, req: CheckRequest) -> CheckResult:
        if req.background and not self._pool.background_ok():
            log.debug("background check for %s skipped: no spare browser slot", req.url.split("?")[0])
            return CheckResult(
                error=WorkerError(type="no_slot", detail="No free browser slot for a background check"),
                mode="browser",
            )
        async with asyncio.timeout(req.timeout_s):
            async with self._pool.acquire() as slot:
                ua = self._pool.user_agent(slot)
                if ua and ua != req.user_agent:
                    return CheckResult(
                        error=WorkerError(type="internal", detail="user agent mismatch"), mode="browser"
                    )
                browser = await self._pool.browser(slot)
                context = await browser.new_context(proxy=pw_proxy(req.proxy))
                try:
                    await context.add_cookies([c.model_dump() for c in req.cookies])
                    page = await context.new_page()
                    tracker = MainResponseTracker(page)
                    await page.goto(req.url, wait_until="domcontentloaded", timeout=req.timeout_s * 1000)
                    return CheckResult(snapshot=await snapshot(page, context, tracker), mode="browser")
                except Exception as e:
                    if isinstance(e, TimeoutError):
                        raise
                    detail = describe_failure((str(e).splitlines() or [""])[0], req.proxy or self._upstream)
                    return CheckResult(error=WorkerError(type="navigation", detail=detail), mode="browser")
                finally:
                    with contextlib.suppress(Exception):
                        await context.close()
