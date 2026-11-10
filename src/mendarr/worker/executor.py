"""Runs one solve request in a browser slot and returns what the page looked like afterwards."""

import asyncio
import base64
import contextlib
import logging
import time

from mendarr.config import Settings
from mendarr.core import judge
from mendarr.plugins import loader
from mendarr.proxy import describe_failure
from mendarr.rpc import Snapshot, SolveRequest, SolveResult, TraceData, WorkerError
from mendarr.sdk import Ctx
from mendarr.worker import backends
from mendarr.worker.egress import Egress
from mendarr.worker.pagestate import (
    MainResponseTracker,
    pw_proxy,
    request_cookies_to_pw,
    snapshot,
)
from mendarr.worker.sessions import SessionManager
from mendarr.worker.slots import SlotPool
from mendarr.worker.tracing import TRACE_GRACE, TraceRecorder
from mendarr.worker.watchdog import Watchdog

log = logging.getLogger("mendarr.worker.executor")

BLOCKED_RESOURCES = frozenset({"image", "stylesheet", "font", "media"})
SNAPSHOT_GRACE = 3.0  # best-effort snapshot after a failure
# the plugin stops this much before the request deadline, so a failure still gets a snapshot to classify
PLUGIN_MARGIN = SNAPSHOT_GRACE + 1.0


class _PluginError(Exception):
    pass


def _no_watchdog(deadline: float) -> contextlib.AbstractContextManager[None]:
    return contextlib.nullcontext()


class Executor:
    def __init__(
        self,
        settings: Settings,
        pool: SlotPool,
        sessions: SessionManager,
        egress: Egress,
        watchdog: Watchdog | None = None,
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.sessions = sessions
        self.egress = egress
        upstream = settings.upstream
        self._upstream = upstream.spec() if upstream else None  # only to name it in errors; browsers have it from launch
        self._plugin_running = watchdog.plugin_running if watchdog else _no_watchdog

    async def solve(self, req: SolveRequest) -> SolveResult:
        deadline = time.monotonic() + req.timeout_s
        egress = await self.egress.current(req.proxy)
        ref = req.plugin.ref if req.plugin else loader.BUILTIN_REF
        try:
            plugin = loader.load_spec(req.plugin)
        except loader.PluginLoadError as e:
            log.warning("plugin rejected: %s", e)
            return SolveResult(plugin=ref, egress=egress, error=WorkerError(type="guard", detail=str(e)))
        backend = plugin.backend
        if req.session and backend != backends.DEFAULT_BACKEND:
            # a session is a context in the camoufox browser; it can't move to another engine
            log.info(
                "session %s stays on %s although %s asks for %s",
                req.session,
                backends.DEFAULT_BACKEND,
                plugin.ref,
                backend,
            )
            backend = backends.DEFAULT_BACKEND
        slot_held = False
        try:
            async with asyncio.timeout(req.timeout_s):
                session = None
                if req.session:
                    session = await self.sessions.get_or_create(
                        req.session, req.session_ttl_minutes, req.proxy
                    )
                async with self.pool.acquire(slot=session.slot if session else None) as slot:
                    slot_held = True
                    res = await self._run(req, slot, session, deadline, egress, plugin, backend)
                    res.plugin = plugin.ref
                    res.user_agent = self.pool.user_agent(slot, backend)
                    return res
        except backends.BackendUnavailable as e:
            return SolveResult(
                plugin=plugin.ref, egress=egress, error=WorkerError(type="internal", detail=str(e))
            )
        except backends.UpstreamUnreachable as e:
            # same kind as an unreachable site: never healed, never counted against the plugin
            return SolveResult(
                plugin=plugin.ref, egress=egress, error=WorkerError(type="navigation", detail=str(e))
            )
        except TimeoutError:
            if slot_held:
                return SolveResult(
                    plugin=plugin.ref, error=WorkerError(type="deadline", detail="Timeout"), egress=egress
                )
            return SolveResult(
                plugin=plugin.ref,
                error=WorkerError(type="no_slot", detail="Timeout waiting for a free browser slot"),
                egress=egress,
            )

    async def _run(self, req, slot, session, deadline, egress, plugin, backend) -> SolveResult:
        PlaywrightError, PlaywrightTimeout = backends.error_types(backend)

        browser = await self.pool.browser(slot, backend)
        context = session.context if session else await browser.new_context(proxy=pw_proxy(req.proxy))
        page = None
        tracker = None
        recorder: TraceRecorder | None = None
        stage = "navigation"
        ctx = None
        challenge_seen = False

        def fail(kind: str, detail: str):
            # late-bound: sees page/ctx/challenge_seen/recorder as far as the run got
            return self._failed(
                req, slot, egress, page, context, tracker, ctx, challenge_seen, kind, detail, recorder
            )
        try:
            page = await context.new_page()
            tracker = MainResponseTracker(page)
            if req.trace:
                recorder = TraceRecorder(page)
            if req.disable_media:

                async def drop_media(route):
                    if route.request.resource_type in BLOCKED_RESOURCES:
                        await route.abort()
                    else:
                        await route.fallback()

                await page.route("**/*", drop_media)
            if req.cookies:
                await context.add_cookies(request_cookies_to_pw(req.cookies, req.url))
            if req.method == "POST":

                async def as_post(route):
                    await route.continue_(
                        method="POST",
                        post_data=req.post_data,
                        headers={
                            **route.request.headers,
                            "content-type": "application/x-www-form-urlencoded",
                        },
                    )

                await page.route(req.url, as_post, times=1)
            remaining_ms = max(1.0, (deadline - time.monotonic()) * 1000)
            await page.goto(req.url, wait_until="domcontentloaded", timeout=remaining_ms)
            first = await snapshot(page, context, tracker)
            challenge_seen = judge.is_challenge(first.headers, first.title, first.html)

            stage = "plugin"
            ctx = Ctx(
                page,
                deadline=deadline - PLUGIN_MARGIN - (TRACE_GRACE if req.trace else 0),
                display=slot,
                click_backend=self.settings.click_backend,
                main_headers=tracker.headers,
            )
            loop_deadline = asyncio.get_running_loop().time() + ctx.remaining()
            try:
                async with asyncio.timeout_at(loop_deadline):
                    with self._plugin_running(ctx.deadline):
                        await plugin.solve(page, ctx)
                    if req.wait_s > 0:
                        await asyncio.sleep(min(req.wait_s, ctx.remaining()))
            except TimeoutError:
                raise
            except Exception as e:
                raise _PluginError(f"{type(e).__name__}: {e}") from e

            stage = "final"
            snap = await snapshot(page, context, tracker)
            shot = None
            if req.screenshot:
                shot = base64.b64encode(await page.screenshot()).decode()
            trace = None
            if judge.is_challenge(snap.headers, snap.title, snap.html) or judge.is_blocked(
                snap.headers, snap.title, snap.html
            ):
                trace = await self._trace(recorder, page, ctx)
            return SolveResult(
                challenge_seen=challenge_seen,
                clicks=ctx.clicks,
                plugin=plugin.ref,
                user_agent=self.pool.user_agent(slot, backend),
                egress=egress,
                snapshot=snap,
                screenshot_b64=shot,
                trace=trace,
                turnstile_token=await ctx.turnstile_token(),
            )
        except (TimeoutError, PlaywrightTimeout):
            return await fail("deadline", "Timeout")
        except _PluginError as e:
            return await fail("plugin", str(e))
        except PlaywrightError as e:
            if stage == "navigation":
                detail = (str(e).splitlines() or [""])[0]
                return await fail("navigation", describe_failure(detail, req.proxy or self._upstream))
            log.exception("browser error during %s", stage)
            return await fail("internal", f"{type(e).__name__}: {e}")
        except Exception as e:
            log.exception("solve failed")
            return await fail("internal", f"{type(e).__name__}: {e}")
        finally:
            with contextlib.suppress(Exception):
                if session is None:
                    await context.close()
                elif page is not None:
                    await page.close()

    async def _failed(
        self, req, slot, egress, page, context, tracker, ctx, challenge_seen, kind, detail, recorder=None
    ) -> SolveResult:
        snap: Snapshot | None = None
        if page is not None and tracker is not None:
            try:
                async with asyncio.timeout(SNAPSHOT_GRACE):
                    snap = await snapshot(page, context, tracker)
            except Exception:
                snap = None
        return SolveResult(
            challenge_seen=challenge_seen,
            clicks=ctx.clicks if ctx is not None else 0,
            user_agent=self.pool.user_agent(slot),
            egress=egress,
            snapshot=snap,
            error=WorkerError(type=kind, detail=detail),
            trace=await self._trace(recorder, page, ctx),
        )

    async def _trace(self, recorder, page, ctx) -> TraceData | None:
        if recorder is None or page is None:
            return None
        steps = ctx.steps if ctx is not None else []
        try:
            async with asyncio.timeout(TRACE_GRACE):
                return await recorder.collect(page, steps)
        except Exception as e:
            log.debug("trace capture incomplete: %s", e)
            return TraceData(console=list(recorder.console), network=list(recorder.network), steps=list(steps))
