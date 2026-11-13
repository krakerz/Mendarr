"""Background re-check of cached cookies: keeps entries alive and finds out early when Cloudflare dropped them."""

import asyncio
import contextlib
import logging
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from mendarr.api.worker_client import WorkerClient, WorkerUnavailable
from mendarr.config import Settings
from mendarr.core.cookies import CacheEntry, CookieCache
from mendarr.core.router import Router
from mendarr.healer.config import ConfigFile, refresh_policy

log = logging.getLogger(__name__)


class ManualSessions(Protocol):
    def sessions(self) -> list: ...
    def should_hold(self, domain: str) -> bool: ...


@dataclass(frozen=True)
class RefreshStatus:
    domain: str
    at: float
    result: str


class Refresher:
    POLL_S = 15.0
    FIRST_ROUND_S = 60.0  # after startup or enabling: not at once, the worker may still be warming up
    JITTER_S = (1.0, 5.0)

    def __init__(
        self,
        settings: Settings,
        router: Router,
        cache: CookieCache,
        worker: WorkerClient,
        config: ConfigFile,
        manual: ManualSessions | None = None,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._settings = settings
        self._router = router
        self._cache = cache
        self._worker = worker
        self._config = config
        self._manual = manual
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._task: asyncio.Task | None = None
        self._status: dict[str, RefreshStatus] = {}
        self.next_at: float | None = None  # None = off

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="cookie-refresher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    def status(self) -> dict[str, RefreshStatus]:
        """Last check per domain, read from the cache rows (survives a restart)."""
        out: dict[str, RefreshStatus] = {}
        for e in self._cache.entries():
            if e.last_check_at > 0 and (e.domain not in out or e.last_check_at > out[e.domain].at):
                out[e.domain] = RefreshStatus(e.domain, e.last_check_at, e.last_check_result)
        out.update(self._status)
        return out

    async def _run(self) -> None:
        interval = 0.0
        while True:
            try:
                interval_s, idle_days = refresh_policy(self._settings, self._config.get())
                now = self._clock()
                if interval_s <= 0:
                    self.next_at = None
                    interval = 0.0
                else:
                    if interval != interval_s or self.next_at is None:
                        # enabled or changed: first round shortly after, then every interval
                        self.next_at = now + min(self.FIRST_ROUND_S, interval_s)
                        interval = interval_s
                    if now >= self.next_at:
                        await self.run_round(interval_s, idle_days)
                        self.next_at = self._clock() + interval_s
                # wake at the round (not up to POLL_S late), but keep polling so policy changes are seen
                due = self.next_at - self._clock() if self.next_at is not None else self.POLL_S
                await self._sleep(max(0.5, min(self.POLL_S, due)))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cookie refresher round failed")
                await self._sleep(self.POLL_S)

    async def run_round(self, interval_s: float, idle_days: float) -> int:
        """One pass over the cached domains; returns how many were checked."""
        try:
            ip = (await self._worker.egress()).ip
        except (WorkerUnavailable, TimeoutError):
            log.debug("cookie refresh: worker unavailable, skipping the round")
            return 0
        if ip == "unknown":
            log.debug("cookie refresh: exit IP not known yet, skipping the round")
            return 0
        now = self._clock()
        results: Counter[str] = Counter()
        checked = 0
        for entry in self._candidates(ip, now, idle_days):
            await self._sleep(self._jitter(*self.JITTER_S))
            try:
                # renew what would expire before the next round; POLL_S covers a late wake-up and a slow round
                result = await self._router.refresh(entry, renew_before=self._clock() + interval_s + self.POLL_S)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cookie refresh of %s failed", entry.domain)
                result = "error"
            log.debug("cookie refresh: domain=%s result=%s", entry.domain, result)
            self._status[entry.domain] = RefreshStatus(entry.domain, self._clock(), result)
            try:
                self._cache.set_check(entry.domain, result)
            except Exception:
                log.exception("could not store the refresh status of %s", entry.domain)
            results[result] += 1
            checked += 1
        if checked:
            log.info("cookie refresh: checked=%d %s", checked, " ".join(f"{k}={v}" for k, v in sorted(results.items())))
        return checked

    def _candidates(self, ip: str, now: float, idle_days: float) -> list[CacheEntry]:
        seen: set[str] = set()
        out: list[CacheEntry] = []
        for e in self._cache.entries():  # newest first
            if e.domain in seen:
                continue
            reason = self._skip_reason(e, ip, now, idle_days)
            if reason is not None:
                log.debug("cookie refresh: skip %s (%s)", e.domain, reason)
                continue
            seen.add(e.domain)
            out.append(e)
        return out

    def _skip_reason(self, e: CacheEntry, ip: str, now: float, idle_days: float) -> str | None:
        if e.egress != ip:
            return "other egress"
        if not e.url:
            return "no url yet"
        if e.expires_at <= now:
            return "expired"
        if e.last_used < now - idle_days * 86400:
            return "idle"
        if self._manual is not None and (
            any(s.active and s.domain == e.domain for s in self._manual.sessions())
            or self._manual.should_hold(e.domain)
        ):
            return "manual solve"
        return None
