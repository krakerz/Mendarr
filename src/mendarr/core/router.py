"""Request flow: cookie-cache check, then a full solve in the worker, then the verdict."""

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from mendarr.api.flaresolverr import MSG_NOT_DETECTED, MSG_SOLVED, failure_message
from mendarr.api.worker_client import WorkerClient, WorkerUnavailable
from mendarr.config import Settings
from mendarr.core.classify import FailureKind, classify
from mendarr.core.cookies import CookieCache
from mendarr.core.domains import hostname, registrable_domain
from mendarr.core.judge import judge
from mendarr.core.traces import FailureReport
from mendarr.plugins.store import PluginStore, RollbackEvent
from mendarr.proxy import redact
from mendarr.rpc import CheckRequest, Cookie, PluginSpec, ProxySpec, SolveRequest, SolveResult

log = logging.getLogger(__name__)


@dataclass
class V1Params:
    url: str
    method: Literal["GET", "POST"]
    post_data: str | None
    max_timeout_ms: int
    cookies: list[dict]
    proxy: ProxySpec | None
    session: str | None
    session_ttl_minutes: int | None
    return_only_cookies: bool
    return_screenshot: bool
    disable_media: bool
    wait_s: float


@dataclass
class Outcome:
    ok: bool
    message: str  # without "Error: "
    url: str = ""
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    html: str = ""
    cookies: list[Cookie] = field(default_factory=list)
    user_agent: str = ""
    screenshot_b64: str | None = None
    turnstile_token: str | None = None
    failure: FailureKind | None = None
    result: SolveResult | None = None  # the worker's raw result (healer: replay traces)


def _req_cookies(p: V1Params) -> list[Cookie]:
    host = hostname(p.url)
    return [
        Cookie(name=str(c["name"]), value=str(c["value"]), domain=str(c.get("domain") or host))
        for c in p.cookies
        if isinstance(c, dict) and c.get("name") is not None and c.get("value") is not None
    ]


def _merge(old: list[Cookie], new: list[Cookie]) -> list[Cookie]:
    merged = {(c.name, c.domain, c.path): c for c in old}
    merged.update({(c.name, c.domain, c.path): c for c in new})
    return list(merged.values())


class RouterHooks(Protocol):
    """Healer side of the request flow. Called synchronously; must not block."""

    def failure(self, report: FailureReport) -> None: ...
    def success(self, domain: str) -> None: ...
    def rollback(self, event: RollbackEvent) -> None: ...


class ManualHold(Protocol):
    """Manual-solve side: requests for a domain waiting on a human solve can be held (core/manual.py)."""

    def should_hold(self, domain: str) -> bool: ...
    async def wait(self, domain: str, timeout: float) -> bool: ...  # True = released by a solve


class Router:
    def __init__(
        self,
        settings: Settings,
        worker: WorkerClient,
        cache: CookieCache,
        clock: Callable[[], float] = time.monotonic,
        store: PluginStore | None = None,
        hooks: RouterHooks | None = None,
    ) -> None:
        self._settings = settings
        self._worker = worker
        self._cache = cache
        self._clock = clock
        self._store = store
        self.hooks = hooks  # set after construction by create_app (healer needs the router)
        self.manual: ManualHold | None = None  # set by create_app

    async def request(self, p: V1Params) -> Outcome:
        deadline = self._clock() + p.max_timeout_ms / 1000

        def remaining() -> float:
            return deadline - self._clock()

        if self.manual is not None:
            domain = registrable_domain(p.url)
            if self.manual.should_hold(domain):
                log.info("holding request for %s until a manual solve", domain)
                if not await self.manual.wait(domain, max(remaining() - 1, 0)):
                    return Outcome(False, failure_message("timeout", "", p.max_timeout_ms), failure="timeout")

        use_cache = self._settings.cookie_cache and not p.session and not p.return_screenshot
        if use_cache:
            cached = await self._try_cache(p, remaining)
            if cached is not None:
                return cached
        return await self._solve(p, remaining, use_cache)

    async def _try_cache(self, p: V1Params, remaining: Callable[[], float]) -> Outcome | None:
        domain = registrable_domain(p.url)
        try:
            egress = f"proxy:{redact(p.proxy.url)}" if p.proxy else (await self._worker.egress()).ip
        except (WorkerUnavailable, TimeoutError):
            return None
        entry = self._cache.get(domain, egress)
        if entry is None or remaining() < 3:
            return None
        try:
            res = await self._worker.check(
                CheckRequest(
                    method=p.method,
                    url=p.url,
                    post_data=p.post_data,
                    cookies=entry.cookies + _req_cookies(p),
                    user_agent=entry.ua,
                    proxy=p.proxy,
                    timeout_s=min(15, remaining() - 1),
                )
            )
        except (WorkerUnavailable, TimeoutError):
            return None
        # an error (UA mismatch, site down, deadline) says nothing about the cookies: keep the entry
        if res.error is not None or res.snapshot is None:
            return None
        snap = res.snapshot
        if not judge(snap, challenge_seen=False).ok:
            self._cache.drop(domain, egress, entry.ua)
            return None
        merged = _merge(entry.cookies, snap.cookies)
        self._cache.put(domain, egress, entry.ua, merged)
        return Outcome(
            ok=True,
            message=MSG_NOT_DETECTED,
            url=snap.url,
            status=snap.status,
            headers=snap.headers,
            html=snap.html,
            cookies=merged,
            user_agent=entry.ua,
        )

    async def _solve(
        self,
        p: V1Params,
        remaining: Callable[[], float],
        use_cache: bool,
        *,
        spec: PluginSpec | None = None,
        record: bool = True,
    ) -> Outcome:
        ms = p.max_timeout_ms
        if spec is None:
            spec = self._resolve_plugin(p.url)
        try:
            res = await self._worker.solve(
                SolveRequest(
                    method=p.method,
                    url=p.url,
                    post_data=p.post_data,
                    cookies=p.cookies,
                    proxy=p.proxy,
                    session=p.session,
                    session_ttl_minutes=p.session_ttl_minutes,
                    timeout_s=max(remaining() - 0.5, 0.1),
                    disable_media=p.disable_media,
                    wait_s=0 if p.return_only_cookies else p.wait_s,
                    screenshot=p.return_screenshot,
                    plugin=spec,
                    trace=True,
                )
            )
        except TimeoutError:
            return Outcome(False, failure_message("timeout", "", ms), failure="timeout")
        except WorkerUnavailable as e:
            return Outcome(False, failure_message("network", f"Worker unavailable: {e}", ms), failure="network")

        snap = res.snapshot
        verdict = judge(snap, challenge_seen=res.challenge_seen) if res.error is None and snap else None
        if verdict is not None and verdict.ok and snap is not None:
            if record:
                self._record(spec, res, ok=True)
                if res.challenge_seen:
                    self._hook("success", registrable_domain(p.url))
            if res.challenge_seen and use_cache:
                self._cache.put(registrable_domain(p.url), res.egress, res.user_agent, snap.cookies)
            return Outcome(
                ok=True,
                message=MSG_SOLVED if res.challenge_seen else MSG_NOT_DETECTED,
                url=snap.url,
                status=snap.status,
                headers=snap.headers,
                html=snap.html,
                cookies=snap.cookies,
                user_agent=res.user_agent,
                screenshot_b64=res.screenshot_b64,
                turnstile_token=res.turnstile_token,
                result=res,
            )

        kind = classify(error=res.error, snapshot=snap, verdict=verdict, clicks=res.clicks)
        if record:
            self._record(spec, res, ok=False, kind=kind)
        if res.error is not None:
            detail = res.error.detail
        elif kind == "network" and snap is not None:
            detail = f"Cloudflare origin error {snap.status}"
        else:
            detail = verdict.reason if verdict is not None else ""
        domain = registrable_domain(p.url)
        log.info("solve failed: domain=%s kind=%s plugin=%s", domain, kind, res.plugin)
        if record:
            self._hook(
                "failure",
                FailureReport(
                    url=p.url, domain=domain, kind=kind, detail=detail, plugin=res.plugin, result=res,
                    session=bool(p.session),
                ),
            )
        return Outcome(False, failure_message(kind, detail, ms), failure=kind, result=res)

    async def replay(
        self,
        url: str,
        spec: PluginSpec,
        *,
        max_timeout_ms: int = 60000,
        method: Literal["GET", "POST"] = "GET",
        post_data: str | None = None,
    ) -> Outcome:
        """Run one plugin version once (healer replay): no cookie cache, no session, no stats."""
        p = V1Params(
            url=url, method=method, post_data=post_data, max_timeout_ms=max_timeout_ms, cookies=[],
            proxy=None, session=None, session_ttl_minutes=None, return_only_cookies=False,
            return_screenshot=False, disable_media=False, wait_s=0,
        )
        deadline = self._clock() + max_timeout_ms / 1000
        return await self._solve(p, lambda: deadline - self._clock(), False, spec=spec, record=False)

    def _resolve_plugin(self, url: str) -> PluginSpec | None:
        if self._store is None:
            return None
        try:
            return self._store.resolve(registrable_domain(url)).spec()
        except sqlite3.Error:
            log.exception("plugin lookup failed; the worker falls back to its builtin")
            return None

    def _record(
        self, spec: PluginSpec | None, res: SolveResult, *, ok: bool, kind: FailureKind | None = None
    ) -> None:
        """Count a run against the plugin version when the outcome says something about the plugin."""
        if spec is None or self._store is None:
            return
        if ok and not res.challenge_seen:
            return  # no challenge: the plugin had nothing to do
        if not ok and (
            kind in ("network", "blocked")
            or (res.error is not None and res.error.type in ("navigation", "no_slot", "internal"))
        ):
            return
        try:
            event = self._store.record(spec.scope, spec.version, ok)
        except sqlite3.Error:
            log.exception("plugin stats update failed")
            return
        if event is not None:
            log.warning(
                "auto-rollback: %s@v%d -> %s (%.0f%% of %d runs ok), incident #%d",
                event.scope, event.from_version,
                f"v{event.to_version}" if event.to_version else "_default",
                event.rate * 100, event.samples, event.incident_id,
            )
            self._hook("rollback", event)

    def _hook(self, name: str, *args: object) -> None:
        if self.hooks is None:
            return
        try:
            getattr(self.hooks, name)(*args)
        except Exception:
            log.exception("router hook %s failed", name)
