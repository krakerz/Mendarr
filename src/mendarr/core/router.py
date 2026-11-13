"""Request flow: cookie-cache check, then a full solve in the worker, then the verdict."""

import contextlib
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import urlsplit

from mendarr.api.flaresolverr import MSG_NOT_DETECTED, MSG_SOLVED, failure_message
from mendarr.api.worker_client import WorkerClient, WorkerUnavailable
from mendarr.config import Settings
from mendarr.core.classify import FailureKind, classify
from mendarr.core.cookies import CacheEntry, CookieCache, lifetime_cookie
from mendarr.core.domains import hostname, registrable_domain
from mendarr.core.judge import judge
from mendarr.core.stats import CHECK_MISMATCH, DomainStats
from mendarr.core.traces import FailureReport
from mendarr.core.tracker import SolveTracker
from mendarr.plugins.store import PluginStore, RollbackEvent
from mendarr.proxy import redact
from mendarr.rpc import CheckRequest, Cookie, PluginSpec, ProxySpec, Snapshot, SolveRequest, SolveResult

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
    via: str = ""  # cache_hit | no_challenge | solved | failed:<kind> | held
    plugin: str = ""
    egress: str = ""


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
        tracker: SolveTracker | None = None,
        stats: DomainStats | None = None,
    ) -> None:
        self._settings = settings
        self._worker = worker
        self._cache = cache
        self._clock = clock
        self._store = store
        self.hooks = hooks  # set after construction by create_app (healer needs the router)
        self.manual: ManualHold | None = None  # set by create_app
        self.tracker = tracker
        self._stats = stats

    async def request(self, p: V1Params) -> Outcome:
        t0 = self._clock()
        out = await self._request(p)
        domain = registrable_domain(p.url)
        log.info(
            "request: domain=%s url=%s via=%s elapsed=%.1fs plugin=%s egress=%s",
            domain, urlsplit(p.url).path or "/", out.via, self._clock() - t0, out.plugin or "-", out.egress or "-",
        )
        if out.via != "held":
            self._stats_call("set_last", domain, out.via)
        return out

    def _stats_call(self, method: str, *args: str) -> None:
        if self._stats is None:
            return
        try:
            getattr(self._stats, method)(*args)
        except sqlite3.Error:
            log.exception("stats write failed")

    async def _request(self, p: V1Params) -> Outcome:
        deadline = self._clock() + p.max_timeout_ms / 1000

        def remaining() -> float:
            return deadline - self._clock()

        if self.manual is not None:
            domain = registrable_domain(p.url)
            if self.manual.should_hold(domain):
                log.info("holding request for %s until a manual solve", domain)
                if not await self.manual.wait(domain, max(remaining() - 1, 0)):
                    return Outcome(
                        False, failure_message("timeout", "", p.max_timeout_ms), failure="timeout", via="held"
                    )

        use_cache = self._settings.cookie_cache and not p.session and not p.return_screenshot
        if use_cache:
            cached, challenged = await self._try_cache(p, remaining)
            if cached is not None:
                return cached
            out = await self._solve(p, remaining, use_cache)
            if challenged and out.via == "no_challenge":
                self._stats_call("bump", registrable_domain(p.url), CHECK_MISMATCH)
            return out
        return await self._solve(p, remaining, use_cache)

    async def _try_cache(self, p: V1Params, remaining: Callable[[], float]) -> tuple[Outcome | None, bool]:
        """(hit, check was challenged)."""
        domain = registrable_domain(p.url)
        try:
            egress = f"proxy:{redact(p.proxy.url)}" if p.proxy else (await self._worker.egress()).ip
        except (WorkerUnavailable, TimeoutError):
            return None, False
        self._cache.touch(domain, p.url)
        entry = self._cache.get(domain, egress)
        if entry is None or remaining() < 3:
            log.debug("cache miss: domain=%s egress=%s", domain, egress)
            return None, False
        snap, challenged = await self._check(entry, p, min(15, remaining() - 1), cookies=_req_cookies(p))
        if challenged:
            log.debug("cache entry for %s was challenged; dropping it", domain)
            self._cache.drop(domain, egress, entry.ua)
            return None, True
        if snap is None:
            return None, False
        merged = _merge(entry.cookies, snap.cookies)
        stored = self._cache.put(domain, egress, entry.ua, merged)
        log.debug("cache hit: domain=%s egress=%s", domain, egress)
        return Outcome(
            ok=True,
            message=MSG_NOT_DETECTED,
            url=snap.url,
            status=snap.status,
            headers=snap.headers,
            html=snap.html,
            cookies=stored.cookies if stored else merged,
            user_agent=entry.ua,
            via="cache_hit",
            egress=egress,
        ), False

    async def _check(
        self, entry: CacheEntry, p: V1Params, timeout_s: float, *, cookies: list[Cookie], background: bool = False
    ) -> tuple[Snapshot | None, bool]:
        """Probe the cached cookies: (snapshot when clean, challenged). (None, False) = inconclusive."""
        try:
            res = await self._worker.check(
                CheckRequest(
                    method=p.method,
                    url=p.url,
                    post_data=p.post_data,
                    cookies=entry.cookies + cookies,
                    user_agent=entry.ua,
                    proxy=p.proxy,
                    timeout_s=timeout_s,
                    background=background,
                )
            )
        except (WorkerUnavailable, TimeoutError):
            return None, False
        # an error (UA mismatch, site down, deadline, no slot) says nothing about the cookies: keep the entry
        if res.error is not None or res.snapshot is None:
            log.debug("cache check for %s inconclusive: %s", entry.domain, res.error.type if res.error else "no snapshot")
            return None, False
        if not judge(res.snapshot, challenge_seen=False).ok:
            return None, True
        return res.snapshot, False

    async def _solve(
        self,
        p: V1Params,
        remaining: Callable[[], float],
        use_cache: bool,
        *,
        spec: PluginSpec | None = None,
        record: bool = True,
        background_of: CacheEntry | None = None,
    ) -> Outcome:
        background = background_of is not None
        ms = p.max_timeout_ms
        if spec is None:
            spec = self._resolve_plugin(p.url)
        domain = registrable_domain(p.url)
        track_ctx = (
            self.tracker.track(
                "solve",
                domain,
                p.url,
                description=f"cookie refresh: {spec.ref if spec else 'resolving'}"
                if background
                else spec.ref
                if spec
                else "resolving",
            )
            if self.tracker is not None and record
            else contextlib.nullcontext()
        )
        with track_ctx:
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
                        background=background,
                    )
                )
            except TimeoutError:
                return Outcome(False, failure_message("timeout", "", ms), failure="timeout", via="failed:timeout")
            except WorkerUnavailable as e:
                return Outcome(
                    False,
                    failure_message("network", f"Worker unavailable: {e}", ms),
                    failure="network",
                    via="failed:network",
                )

            snap = res.snapshot
            verdict = judge(snap, challenge_seen=res.challenge_seen) if res.error is None and snap else None
            if verdict is not None and verdict.ok and snap is not None:
                if record and not background:
                    self._record(spec, res, ok=True)
                    if res.challenge_seen:
                        self._hook("success", registrable_domain(p.url))
                if res.challenge_seen and use_cache:
                    self._cache.put(
                        registrable_domain(p.url),
                        res.egress,
                        res.user_agent,
                        snap.cookies,
                        url=p.url,
                        last_used=background_of.last_used if background_of else None,
                    )
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
                    via="solved" if res.challenge_seen else "no_challenge",
                    plugin=res.plugin,
                    egress=res.egress,
                )

            kind = classify(error=res.error, snapshot=snap, verdict=verdict, clicks=res.clicks)
            if record and not background:
                self._record(spec, res, ok=False, kind=kind)
            if res.error is not None:
                detail = res.error.detail
            elif kind == "network" and snap is not None:
                detail = f"Cloudflare origin error {snap.status}"
            else:
                detail = verdict.reason if verdict is not None else ""
            domain = registrable_domain(p.url)
            log.info("solve failed: domain=%s kind=%s plugin=%s", domain, kind, res.plugin)
            if record and not (background and res.error is not None and res.error.type == "no_slot"):
                self._hook(
                    "failure",
                    FailureReport(
                        url=p.url, domain=domain, kind=kind, detail=detail, plugin=res.plugin, result=res,
                        session=bool(p.session), background=background,
                    ),
                )
            return Outcome(
                False,
                failure_message(kind, detail, ms),
                failure=kind,
                result=res,
                via=f"failed:{kind}",
                plugin=res.plugin,
                egress=res.egress,
            )

    async def refresh(self, entry: CacheEntry, *, renew_before: float) -> str:
        """Background re-check of one cached entry (cookie refresher). Returns the status word shown on the page."""
        p = V1Params(
            url=entry.url, method="GET", post_data=None, max_timeout_ms=60000, cookies=[], proxy=None,
            session=None, session_ttl_minutes=None, return_only_cookies=False, return_screenshot=False,
            disable_media=False, wait_s=0,
        )
        deadline = self._clock() + p.max_timeout_ms / 1000

        def remaining() -> float:
            return deadline - self._clock()

        snap, challenged = await self._check(entry, p, min(15, remaining() - 1), cookies=[], background=True)
        if challenged:
            out = await self._solve(p, remaining, True, background_of=entry)
            if out.result is not None and out.result.error is not None and out.result.error.type == "no_slot":
                return "no_slot"  # keep the entry for the next round
            if out.via == "solved":
                if (out.egress, out.user_agent) != (entry.egress, entry.ua):
                    self._cache.drop(entry.domain, entry.egress, entry.ua)
                return "solved"
            self._cache.drop(entry.domain, entry.egress, entry.ua)
            if out.ok:
                self._stats_call("bump", entry.domain, CHECK_MISMATCH)
                return "no_challenge"
            return "solve_failed"
        if snap is None:
            return "error"
        stored = self._cache.put(entry.domain, entry.egress, entry.ua, _merge(entry.cookies, snap.cookies))
        if stored is not None and lifetime_cookie(stored) == "cf_clearance" and stored.expires_at < renew_before:
            out = await self._solve(p, remaining, True, background_of=stored)
            if out.via == "solved":
                return "renewed"
            if out.result is not None and out.result.error is not None and out.result.error.type == "no_slot":
                return "no_slot"
        return "ok"

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
