"""Browser slots: one lazily launched browser per X display and backend, one request at a time."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import asynccontextmanager
from typing import Any

from mendarr.proxy import UpstreamProxy, curl_proxy_url, is_upstream
from mendarr.rpc import ProxySpec
from mendarr.worker.backends import DEFAULT_BACKEND, BackendUnavailable
from mendarr.worker.backends.camoufox import Launcher, LaunchedBrowser

log = logging.getLogger("mendarr.worker.slots")


class SlotTimeout(Exception):
    pass


Key = tuple[int, str, str]  # (slot, backend, proxy key); "" = the backend's default process


class SlotPool:
    def __init__(
        self,
        count: int,
        launchers: Launcher | Mapping[str, Launcher],
        *,
        per_proxy: Collection[str] = (),
        upstream: UpstreamProxy | None = None,
    ) -> None:
        self._indexes = list(range(1, count + 1))  # slot numbers = X display numbers
        self._launchers = dict(launchers) if isinstance(launchers, Mapping) else {DEFAULT_BACKEND: launchers}
        self._busy: set[int] = set()
        self._cond = asyncio.Condition()
        self._launched: dict[Key, LaunchedBrowser] = {}
        self._deferred: set[Key] = set()  # already logged "language waits" per browser
        self._launch_locks = {i: asyncio.Lock() for i in self._indexes}
        # backends whose engine ignores a context proxy: a request proxy gets its own process (obscura)
        self._per_proxy = frozenset(per_proxy)
        self._upstream = upstream
        self._generation: dict[str, int] = {}  # bumped by retire(): an updated backend binary
        self._launched_gen: dict[Key, int] = {}

    @property
    def indexes(self) -> list[int]:
        return list(self._indexes)

    def has_free(self) -> bool:
        return len(self._busy) < len(self._indexes)

    def background_ok(self) -> bool:
        # a background job leaves one slot free for real requests (unless there is only one slot)
        free = len(self._indexes) - len(self._busy)
        return free >= 2 or (len(self._indexes) == 1 and free == 1)

    def _key(self, slot: int, backend: str, proxy: ProxySpec | None) -> Key:
        if backend not in self._per_proxy or proxy is None:
            return (slot, backend, "")
        if self._upstream and is_upstream(proxy, self._upstream.server, self._upstream.username):
            return (slot, backend, "")
        return (slot, backend, curl_proxy_url(proxy))  # never logged: holds credentials

    def idle(self) -> bool:
        return not self._busy

    def retire(self, backend: str) -> None:
        self._generation[backend] = self._generation.get(backend, 0) + 1
        log.info("backend %s updated: idle browsers relaunch on next use", backend)

    @asynccontextmanager
    async def acquire(self, slot: int | None = None) -> AsyncIterator[int]:
        # Callers bound the wait with asyncio.timeout
        async with self._cond:
            while True:
                if slot is not None:
                    free = slot if slot not in self._busy else None
                else:
                    free = next((i for i in self._indexes if i not in self._busy), None)
                if free is not None:
                    break
                await self._cond.wait()
            self._busy.add(free)
        try:
            yield free
        finally:
            async with self._cond:
                self._busy.discard(free)
                self._cond.notify_all()

    async def _close_other_proxies(self, key: Key) -> None:
        for other in [k for k in self._launched if k[:2] == key[:2] and k[2] and k[2] != key[2]]:
            launched = self._launched.pop(other)
            self._launched_gen.pop(other, None)
            self._deferred.discard(other)
            with contextlib.suppress(Exception):
                await launched.close()

    async def browser(
        self, slot: int, backend: str = DEFAULT_BACKEND, locale: str | None = None, proxy: ProxySpec | None = None
    ) -> Any:
        """The slot's browser for the backend, launched on first use. `locale` is the Settings-page language
        of the request (None = none set): a differing one relaunches an idle browser, never a busy one.
        `proxy` = the request's proxy: a per-proxy backend runs it on its own process."""
        launcher = self._launchers.get(backend)
        if launcher is None:
            raise BackendUnavailable(f"backend {backend!r} is not available in this worker")
        key = self._key(slot, backend, proxy)
        async with self._launch_locks[slot]:
            if key[2]:
                await self._close_other_proxies(key)  # one per-proxy process per slot and backend
            current = self._launched.get(key)
            if current is not None and not current.browser.is_connected():
                log.warning("slot %s %s browser disconnected, relaunching", slot, backend)
                with contextlib.suppress(Exception):
                    await current.close()
                current = None
            if current is not None and self._launched_gen.get(key, 0) != self._generation.get(backend, 0):
                if len(current.browser.contexts) <= current.base_contexts:
                    log.info("slot %s %s: relaunching on the updated backend", slot, backend)
                    with contextlib.suppress(Exception):
                        await current.close()
                    current = None
            if current is not None and locale and locale != current.locale:
                if len(current.browser.contexts) <= current.base_contexts:
                    log.info("slot %s %s: relaunching for browser language %s", slot, backend, locale)
                    with contextlib.suppress(Exception):
                        await current.close()
                    current = None
                elif key not in self._deferred:
                    self._deferred.add(key)
                    log.info(
                        "slot %s %s: language %s waits until the browser has no open session", slot, backend, locale
                    )
            if current is None:
                current = await (launcher(slot, locale=locale, proxy=proxy) if key[2] else launcher(slot, locale=locale))
                self._launched[key] = current
                self._launched_gen[key] = self._generation.get(backend, 0)
                self._deferred.discard(key)
            return current.browser

    def user_agent(self, slot: int, backend: str = DEFAULT_BACKEND, proxy: ProxySpec | None = None) -> str:
        launched = self._launched.get(self._key(slot, backend, proxy))
        return launched.user_agent if launched else ""

    def locale(self, slot: int, backend: str = DEFAULT_BACKEND, proxy: ProxySpec | None = None) -> str | None:
        launched = self._launched.get(self._key(slot, backend, proxy))
        return launched.locale if launched else None

    def context_options(
        self, slot: int, backend: str = DEFAULT_BACKEND, locale: str | None = None, proxy: ProxySpec | None = None
    ) -> dict[str, str]:
        """new_context kwargs for the backend's language; Camoufox sets it at launch (a context override could
        fight its fingerprint config), the Chromium backends need it per context; the request's language wins."""
        locale = locale or self.locale(slot, backend, proxy)
        return {"locale": locale} if locale and backend != DEFAULT_BACKEND else {}

    async def warm(self, slot: int = 1) -> None:
        try:
            await self.browser(slot)
        except Exception:
            log.exception("warming slot %s failed", slot)

    async def close(self) -> None:
        for launched in list(self._launched.values()):
            with contextlib.suppress(Exception):
                await launched.close()
        self._launched.clear()
        self._launched_gen.clear()
