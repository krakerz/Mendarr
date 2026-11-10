"""FlareSolverr-style sessions: a long-lived browser context pinned to one slot."""

import contextlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mendarr.rpc import ProxySpec
from mendarr.worker.pagestate import pw_proxy
from mendarr.worker.slots import SlotPool


@dataclass
class Session:
    id: str
    slot: int
    context: Any
    created: float
    proxy: ProxySpec | None


class SessionManager:
    def __init__(self, pool: SlotPool, clock: Callable[[], float] = time.monotonic) -> None:
        self._pool = pool
        self._clock = clock
        self._sessions: dict[str, Session] = {}

    async def create(
        self, session_id: str | None, proxy: ProxySpec | None = None
    ) -> tuple[Session, bool]:
        if session_id is not None and session_id in self._sessions:
            return self._sessions[session_id], False
        sid = session_id or str(uuid.uuid1())
        counts = {i: 0 for i in self._pool.indexes}
        for s in self._sessions.values():
            counts[s.slot] = counts.get(s.slot, 0) + 1
        slot = min(counts, key=lambda i: (counts[i], i))
        browser = await self._pool.browser(slot)
        context = await browser.new_context(proxy=pw_proxy(proxy))
        session = Session(sid, slot, context, self._clock(), proxy)
        self._sessions[sid] = session
        return session, True

    async def get_or_create(
        self, session_id: str, ttl_minutes: int | None, proxy: ProxySpec | None
    ) -> Session:
        session = self._sessions.get(session_id)
        if session is not None:
            expired = ttl_minutes is not None and self._clock() - session.created > ttl_minutes * 60
            browser = getattr(session.context, "browser", None)
            gone = browser is None or not browser.is_connected()
            if expired or gone:
                await self.destroy(session_id)
                session = None
        if session is None:
            session, _ = await self.create(session_id, proxy)
        return session

    def ids(self) -> list[str]:
        return list(self._sessions)

    async def destroy(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        with contextlib.suppress(Exception):
            await session.context.close()
        return True

    async def close_all(self) -> None:
        for sid in self.ids():
            await self.destroy(sid)
