"""Manual-solve sessions: a reserved slot showing the URL headed, kept open until the API finishes or cancels."""

import asyncio
import contextlib
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from mendarr.core import judge
from mendarr.rpc import ManualResult, ManualStart, ManualStarted, ManualState, Snapshot, TraceData
from mendarr.worker.demo import DemoRecorder
from mendarr.worker.egress import Egress
from mendarr.worker.pagestate import MainResponseTracker, pw_proxy, snapshot
from mendarr.worker.slots import SlotPool
from mendarr.worker.tracing import TraceRecorder

log = logging.getLogger("mendarr.worker.manual")

EXPIRY_GRACE = 30.0
STATE_HTML_CHARS = 300_000


class NoFreeSlot(Exception):
    pass


@dataclass(eq=False)
class _Session:
    id: str
    slot: int
    url: str
    context: Any
    page: Any
    tracker: MainResponseTracker
    recorder: TraceRecorder
    demo: DemoRecorder
    stack: contextlib.AsyncExitStack
    egress: str
    user_agent: str
    challenge_seen: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    vnc_connected: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task] = field(default_factory=list)


class ManualSessions:
    def __init__(self, pool: SlotPool, egress: Egress) -> None:
        self.pool = pool
        self.egress = egress
        self._sessions: dict[str, _Session] = {}

    async def start(self, req: ManualStart) -> ManualStarted:
        stack = contextlib.AsyncExitStack()
        try:
            slot = await asyncio.wait_for(stack.enter_async_context(self.pool.acquire()), req.slot_wait_s)
        except TimeoutError:
            await stack.aclose()
            raise NoFreeSlot from None
        context = None
        try:
            egress = await self.egress.current(req.proxy)
            browser = await self.pool.browser(slot, req.backend)
            context = await browser.new_context(proxy=pw_proxy(req.proxy))
            demo = DemoRecorder()
            await demo.install(context)
            page = await context.new_page()
            demo.watch(page)
            s = _Session(
                id=secrets.token_hex(8),
                slot=slot,
                url=req.url,
                context=context,
                page=page,
                tracker=MainResponseTracker(page),
                recorder=TraceRecorder(page),
                demo=demo,
                stack=stack,
                egress=egress,
                user_agent=self.pool.user_agent(slot, req.backend),
            )
            context.on("page", lambda p: self._on_new_page(s, p))
        except BaseException:
            if context is not None:
                with contextlib.suppress(Exception):
                    await context.close()
            await stack.aclose()
            raise
        self._sessions[s.id] = s
        s.tasks = [
            asyncio.create_task(self._goto(s)),
            asyncio.create_task(self._expire(s.id, req.timeout_s + EXPIRY_GRACE)),
        ]
        return ManualStarted(id=s.id, slot=slot, user_agent=s.user_agent, egress=egress)

    def _on_new_page(self, s: _Session, page: Any) -> None:
        s.page = page
        s.tracker = MainResponseTracker(page)
        s.demo.watch(page)

    async def _goto(self, s: _Session) -> None:
        try:
            # Give the user's VNC client up to 8s to connect first so the human sees the initial navigation
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(s.vnc_connected.wait(), timeout=8.0)
            await s.page.goto(s.url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            log.info("manual %s: initial navigation: %s", s.id, e)

    async def _expire(self, sid: str, delay: float) -> None:
        await asyncio.sleep(delay)
        log.warning("manual session %s expired", sid)
        await self.cancel(sid)

    def _current_page(self, s: _Session) -> Any:
        if not s.page.is_closed():
            return s.page
        for p in reversed(list(s.context.pages)):
            if not p.is_closed():
                s.page = p
                s.tracker = MainResponseTracker(p)
                return p
        return None

    async def _snap(self, s: _Session, page: Any, timeout: float) -> Snapshot | None:
        try:
            snap = await asyncio.wait_for(snapshot(page, s.context, s.tracker), timeout)
        except Exception:  # a navigating page throws
            return None
        if judge.is_challenge(snap.headers, snap.title, snap.html):
            s.challenge_seen = True
        return snap

    async def state(self, sid: str) -> ManualState | None:
        s = self._sessions.get(sid)
        if s is None:
            return None
        async with s.lock:
            page = self._current_page(s)
            if page is None:
                return ManualState(id=s.id, status="closed", challenge_seen=s.challenge_seen, events=len(s.demo.events))
            snap = await self._snap(s, page, 5)
            if snap is not None:
                snap.html = snap.html[:STATE_HTML_CHARS]
            return ManualState(
                id=s.id, status="running", challenge_seen=s.challenge_seen, snapshot=snap, events=len(s.demo.events)
            )

    async def finish(self, sid: str) -> ManualResult | None:
        s = self._sessions.get(sid)
        if s is None:
            return None
        async with s.lock:
            page = self._current_page(s)
            snap = await self._snap(s, page, 10) if page is not None else None
            cookies: list[dict] = []
            with contextlib.suppress(Exception):
                cookies = await s.context.cookies()
            demo = await s.demo.summary(page.url if page is not None else s.url, cookies)
            steps = s.demo.steps()
            trace = TraceData(steps=steps)
            if page is not None:
                try:
                    trace = await s.recorder.collect(page, steps)
                except Exception as e:
                    log.debug("manual %s: trace collect failed: %s", s.id, e)
            await self._close(s)
            return ManualResult(
                challenge_seen=s.challenge_seen,
                snapshot=snap,
                user_agent=s.user_agent,
                egress=s.egress,
                demo=demo,
                trace=trace,
            )

    async def cancel(self, sid: str) -> bool:
        s = self._sessions.get(sid)
        if s is None:
            return False
        await self._close(s)
        return True

    async def _close(self, s: _Session) -> None:
        if self._sessions.pop(s.id, None) is None:
            return
        s.ended.set()
        me = asyncio.current_task()
        for t in s.tasks:
            if t is not me:
                t.cancel()
        with contextlib.suppress(Exception):
            await s.context.close()
        with contextlib.suppress(Exception):
            await s.stack.aclose()

    def for_slot(self, slot: int) -> _Session | None:
        return next((s for s in self._sessions.values() if s.slot == slot), None)

    async def close_all(self) -> None:
        for s in list(self._sessions.values()):
            await self._close(s)
