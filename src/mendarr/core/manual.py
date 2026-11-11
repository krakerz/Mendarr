"""Manual solves: a human drives the worker's real browser over VNC; the core judges, caches and records.

Attempt results added here: manual_solved, manual_expired, manual_cancelled, manual_failed
(M3 documents the rest of the vocabulary in plugins/store.py).
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

from mendarr.api.worker_client import WorkerClient, WorkerRejected, WorkerUnavailable
from mendarr.core.cookies import CookieCache
from mendarr.core.domains import registrable_domain
from mendarr.core.judge import is_challenge, judge
from mendarr.core.traces import TraceStore
from mendarr.plugins.store import OPEN_INCIDENTS, PluginStore
from mendarr.rpc import ManualResult, ManualStart, Snapshot, SolveResult

log = logging.getLogger(__name__)

Status = Literal["running", "solved", "expired", "cancelled", "failed"]
TERMINAL = ("solved", "expired", "cancelled", "failed")
KEEP_FINISHED = 50
MAX_POLL_ERRORS = 20


class ManualError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status  # HTTP status for the UI (404, 409, 503)


@dataclass
class ManualSession:
    id: str  # the worker's session id (also the UI's)
    incident_id: int | None = None
    domain: str = ""
    url: str = ""
    slot: int = 0
    deadline: float = 0.0
    started: float = 0.0
    status: Status = "running"
    detail: str = "solve the challenge in the live view"
    user_agent: str = ""
    egress: str = ""
    events: int = 0
    trace_id: str = ""
    ended: float | None = None
    challenge_seen: bool = False
    task: asyncio.Task | None = field(default=None, repr=False)
    backend: str = "camoufox"

    @property
    def active(self) -> bool:
        return self.status == "running"


class ManualManager:
    def __init__(
        self,
        worker: WorkerClient,
        store: PluginStore,
        cache: CookieCache,
        traces: TraceStore,
        *,
        hold_policy: Callable[[str], bool] = lambda d: False,
        give_up_s: Callable[[], float] = lambda: 604800.0,
        timeout_s: float = 600.0,
        poll_s: float = 1.5,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._worker = worker
        self._store = store
        self._cache = cache
        self._traces = traces
        self._hold_policy = hold_policy
        self._give_up_s = give_up_s
        self._timeout_s = timeout_s
        self._poll_s = poll_s
        self._clock = clock
        self._sessions: dict[str, ManualSession] = {}
        self._holds: dict[str, asyncio.Event] = {}

    async def start(self, incident_id: int, backend: str = "camoufox") -> ManualSession:
        inc = self._store.incident(incident_id)
        if inc is None:
            raise ManualError(404, "no such incident")
        if inc["state"] not in OPEN_INCIDENTS:
            raise ManualError(409, f"incident is {inc['state']}")
        if not inc["url"]:
            raise ManualError(409, "incident has no URL to open")
        for s in self._sessions.values():
            if s.active and (s.incident_id == incident_id or s.domain == inc["domain"]):
                return s
        try:
            started = await self._worker.manual_start(
                ManualStart(url=inc["url"], backend=backend, timeout_s=self._timeout_s)
            )
        except WorkerRejected as e:
            if e.status == 409:
                raise ManualError(409, "no free browser slot, try again when a solve finishes") from e
            raise ManualError(503, f"worker unavailable: {e}") from e
        except (WorkerUnavailable, TimeoutError) as e:
            raise ManualError(503, f"worker unavailable: {e}") from e
        now = self._clock()
        if inc["state"] != "manual":
            self._store.update_incident(
                incident_id, state="manual", tier=3, next_attempt_at=now + self._give_up_s(),
                note="manual solve started",
            )
        s = ManualSession(
            id=started.id,
            incident_id=incident_id,
            domain=inc["domain"],
            url=inc["url"],
            slot=started.slot,
            started=now,
            deadline=now + self._timeout_s,
            user_agent=started.user_agent,
            egress=started.egress,
            backend=backend,
        )
        self._sessions[s.id] = s
        s.task = asyncio.create_task(self._watch(s))
        log.info("manual solve %s started for %s (incident %d, slot %d)", s.id, s.domain, incident_id, s.slot)
        return s

    async def start_adhoc(self, url: str, backend: str = "camoufox") -> ManualSession:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"invalid URL: {url!r}")
        domain = registrable_domain(url)
        if not domain:
            raise ValueError(f"could not determine registrable domain: {url!r}")
        try:
            started = await self._worker.manual_start(
                ManualStart(url=url, backend=backend, timeout_s=self._timeout_s)
            )
        except WorkerRejected as e:
            if e.status == 409:
                raise ManualError(409, "no free browser slot, try again when a solve finishes") from e
            raise ManualError(503, f"worker unavailable: {e}") from e
        except (WorkerUnavailable, TimeoutError) as e:
            raise ManualError(503, f"worker unavailable: {e}") from e
        now = self._clock()
        s = ManualSession(
            id=started.id,
            incident_id=None,
            domain=domain,
            url=url,
            slot=started.slot,
            started=now,
            deadline=now + self._timeout_s,
            user_agent=started.user_agent,
            egress=started.egress,
            backend=backend,
        )
        self._sessions[s.id] = s
        s.task = asyncio.create_task(self._watch(s))
        log.info("ad-hoc manual solve %s started for %s (slot %d, backend %s)", s.id, domain, s.slot, backend)
        return s

    def get(self, sid: str) -> ManualSession | None:
        return self._sessions.get(sid)

    def sessions(self) -> list[ManualSession]:
        return sorted(self._sessions.values(), key=lambda s: s.started, reverse=True)

    def clear_finished(self) -> int:
        to_remove = [sid for sid, s in self._sessions.items() if not s.active]
        for sid in to_remove:
            del self._sessions[sid]
        return len(to_remove)

    def active_for_slot(self, slot: int) -> ManualSession | None:
        return next((s for s in self._sessions.values() if s.active and s.slot == slot), None)

    async def finish(self, sid: str) -> bool:
        s = self._sessions.get(sid)
        if s is None or not s.active:
            return False
        await self._stop_task(s)
        if s.active:
            await self._succeed(s, None)
        return True

    async def cancel(self, sid: str) -> bool:
        s = self._sessions.get(sid)
        if s is None or not s.active:
            return False
        await self._stop_task(s)
        if s.active:  # the watcher may have finished the session while we cancelled it
            await self._end(s, "cancelled", "cancelled from the dashboard")
        return True

    def should_hold(self, domain: str) -> bool:
        try:
            on = self._hold_policy(domain)
        except Exception:
            log.exception("manual hold policy failed")
            return False
        if not on:
            return False
        return any(i["domain"] == domain and i["state"] == "manual" for i in self._store.open_incidents())

    async def wait(self, domain: str, timeout: float) -> bool:
        if timeout <= 0:
            return False
        ev = self._holds.setdefault(domain, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def aclose(self) -> None:
        for s in list(self._sessions.values()):
            if not s.active:
                continue
            await self._stop_task(s)
            with contextlib.suppress(Exception):
                await self._worker.manual_cancel(s.id)

    # --- internals ---

    @staticmethod
    async def _stop_task(s: ManualSession) -> None:
        t = s.task
        if t is None or t is asyncio.current_task():
            return
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t

    async def _watch(self, s: ManualSession) -> None:
        try:
            ok_streak = 0
            errors = 0
            while True:
                await asyncio.sleep(self._poll_s)
                if self._clock() >= s.deadline:
                    await self._end(s, "expired", "no solve in time")
                    return
                if s.incident_id is not None:
                    inc = self._store.incident(s.incident_id)
                    if inc is None or inc["state"] not in OPEN_INCIDENTS:
                        await self._end(s, "cancelled", "incident closed meanwhile")
                        return
                try:
                    st = await self._worker.manual_state(s.id)
                except WorkerRejected as e:
                    if e.status == 404:
                        self._fail(s, "worker lost the session")
                        return
                    errors += 1
                    if errors >= MAX_POLL_ERRORS:
                        await self._end(s, "failed", "worker unreachable")
                        return
                    continue
                except (WorkerUnavailable, TimeoutError):
                    errors += 1
                    if errors >= MAX_POLL_ERRORS:
                        await self._end(s, "failed", "worker unreachable")
                        return
                    continue
                errors = 0
                s.events = st.events
                if st.status == "closed":
                    await self._end(s, "cancelled", "browser tab closed")
                    return
                is_chal = st.challenge_seen or (
                    st.snapshot is not None
                    and is_challenge(st.snapshot.headers, st.snapshot.title, st.snapshot.html)
                )
                if is_chal:
                    s.challenge_seen = True

                is_clear = self._clear(s, st.snapshot, s.challenge_seen)
                ok_streak = ok_streak + 1 if is_clear else 0

                # Auto-solve only if a challenge was actually detected and cleared for 2 consecutive polls
                if s.challenge_seen and ok_streak >= 2:
                    await self._succeed(s, st.snapshot)
                    return

                if s.challenge_seen:
                    s.detail = "challenge detected — solve it in the live view"
                elif ok_streak:
                    s.detail = "page loaded — interact in live view or click Finish & Capture"
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("manual solve %s crashed", s.id)
            self._fail(s, "internal error")

    @staticmethod
    def _clear(s: ManualSession, snap: Snapshot | None, seen: bool) -> bool:
        return (
            snap is not None
            and snap.url.startswith(("http://", "https://"))
            and registrable_domain(snap.url) == s.domain
            and judge(snap, challenge_seen=seen).ok
        )

    async def _succeed(self, s: ManualSession, last_snap: Snapshot | None) -> None:
        try:
            res = await self._worker.manual_finish(s.id)
        except Exception as e:
            log.warning("manual finish for %s failed (%s); using the last polled state", s.id, e)
            res = ManualResult(snapshot=last_snap)
        snap = res.snapshot if self._clear(s, res.snapshot, res.challenge_seen or s.challenge_seen) else last_snap
        if snap is None:
            snap = res.snapshot
        if snap is None:  # not reachable: _watch only gets here after a clear poll
            self._fail(s, "no page state to cache")
            return
        ua = res.user_agent or s.user_agent
        egress = res.egress or s.egress
        self._cache.put(s.domain, egress, ua, snap.cookies)
        trace_id = (
            self._traces.save(
                domain=s.domain,
                url=s.url,
                kind="demo",
                result=SolveResult(
                    challenge_seen=res.challenge_seen,
                    plugin="manual",
                    user_agent=ua,
                    egress=egress,
                    snapshot=snap,
                    trace=res.trace,
                ),
                detail="manual solve",
                plugin="manual",
                incident_id=s.incident_id,
                extra={"demo": res.demo},
            )
            or ""
        )
        now = self._clock()
        if s.incident_id is not None:
            self._store.update_incident(s.incident_id, state="solved", next_attempt_at=None, note="solved manually")
            self._store.resolve_domain(s.domain, "solved manually")
            self._store.add_attempt(
                s.incident_id,
                tier=3,
                result="manual_solved",
                detail=f"{len(res.demo.get('events', []))} demo events",
                trace_id=trace_id,
                elapsed_s=now - s.started,
            )
        else:
            self._store.resolve_domain(s.domain, "solved manually")
        s.trace_id = trace_id
        self._mark(s, "solved", "solved manually")
        ev = self._holds.pop(s.domain, None)
        if ev is not None:
            ev.set()
        log.info("manual solve %s for %s succeeded", s.id, s.domain)

    async def _end(self, s: ManualSession, status: Status, detail: str) -> None:
        with contextlib.suppress(WorkerUnavailable, TimeoutError):
            await self._worker.manual_cancel(s.id)
        if s.incident_id is not None:
            try:
                self._store.add_attempt(
                    s.incident_id, tier=3, result=f"manual_{status}", detail=detail,
                    elapsed_s=self._clock() - s.started,
                )
            except Exception:
                log.exception("could not record the manual attempt for %s", s.id)
        self._mark(s, status, detail)

    def _fail(self, s: ManualSession, detail: str) -> None:
        if s.incident_id is not None:
            try:
                self._store.add_attempt(
                    s.incident_id, tier=3, result="manual_failed", detail=detail, elapsed_s=self._clock() - s.started
                )
            except Exception:
                log.exception("could not record the manual attempt for %s", s.id)
        self._mark(s, "failed", detail)

    def _mark(self, s: ManualSession, status: Status, detail: str) -> None:
        s.status = status
        s.detail = detail
        s.ended = self._clock()
        finished = [x for x in self._sessions.values() if not x.active]
        finished.sort(key=lambda x: x.ended or 0.0)
        for old in finished[: max(len(finished) - KEEP_FINISHED, 0)]:
            self._sessions.pop(old.id, None)
