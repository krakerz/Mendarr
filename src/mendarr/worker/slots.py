"""Browser slots: one lazily launched browser per X display and backend, one request at a time."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from mendarr.worker.backends import DEFAULT_BACKEND, BackendUnavailable
from mendarr.worker.backends.camoufox import Launcher, LaunchedBrowser

log = logging.getLogger("mendarr.worker.slots")


class SlotTimeout(Exception):
    pass


class SlotPool:
    def __init__(self, count: int, launchers: Launcher | Mapping[str, Launcher]) -> None:
        self._indexes = list(range(1, count + 1))  # slot numbers = X display numbers
        self._launchers = dict(launchers) if isinstance(launchers, Mapping) else {DEFAULT_BACKEND: launchers}
        self._busy: set[int] = set()
        self._cond = asyncio.Condition()
        self._launched: dict[tuple[int, str], LaunchedBrowser] = {}
        self._launch_locks = {i: asyncio.Lock() for i in self._indexes}

    @property
    def indexes(self) -> list[int]:
        return list(self._indexes)

    def has_free(self) -> bool:
        return len(self._busy) < len(self._indexes)

    def background_ok(self) -> bool:
        # a background job leaves one slot free for real requests (unless there is only one slot)
        free = len(self._indexes) - len(self._busy)
        return free >= 2 or (len(self._indexes) == 1 and free == 1)

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

    async def browser(self, slot: int, backend: str = DEFAULT_BACKEND) -> Any:
        launcher = self._launchers.get(backend)
        if launcher is None:
            raise BackendUnavailable(f"backend {backend!r} is not available in this worker")
        key = (slot, backend)
        async with self._launch_locks[slot]:
            current = self._launched.get(key)
            if current is not None and not current.browser.is_connected():
                log.warning("slot %s %s browser disconnected, relaunching", slot, backend)
                with contextlib.suppress(Exception):
                    await current.close()
                current = None
            if current is None:
                current = await launcher(slot)
                self._launched[key] = current
            return current.browser

    def user_agent(self, slot: int, backend: str = DEFAULT_BACKEND) -> str:
        launched = self._launched.get((slot, backend))
        return launched.user_agent if launched else ""

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
