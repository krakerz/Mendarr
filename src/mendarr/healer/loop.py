"""The healer: one background task that retries, asks the local LLM for a fixed plugin, replays and promotes it."""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Coroutine, Iterable
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from mendarr.config import Settings
from mendarr.core.router import Router
from mendarr.core.traces import FailureReport, TraceStore
from mendarr.core.tracker import SolveTracker
from mendarr.healer import prompt
from mendarr.healer.config import ConfigFile, DomainPolicy, HealerConfig
from mendarr.healer.llm import Endpoint, LlmClient, LlmUnavailable
from mendarr.healer.notify import Event, Notice, Notifier
from mendarr.plugins.guard import GuardError
from mendarr.plugins.store import Author, PluginRecord, PluginStore, RollbackEvent, StoreError
from mendarr.rpc import PluginSpec

log = logging.getLogger(__name__)

HEALABLE = frozenset({"challenge_loop", "unknown"})
_ALL_CHANNELS = ("discord", "webhook")


def healable(report: FailureReport) -> bool:
    res = report.result
    return (
        report.kind in HEALABLE
        or (report.kind == "timeout" and res is not None and res.challenge_seen)
        or (res is not None and res.error is not None and res.error.type in ("plugin", "guard"))
    )


def _next_midnight(now: float) -> float:
    return datetime.combine(date.fromtimestamp(now) + timedelta(days=1), datetime.min.time()).timestamp()


class Healer:
    def __init__(
        self,
        settings: Settings,
        store: PluginStore,
        router: Router,
        traces: TraceStore,
        config: ConfigFile,
        *,
        llm: LlmClient | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], float] = time.time,
        heal_dir: Path | None = None,
        tracker: SolveTracker | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._router = router
        self._traces = traces
        self._config = config
        self._llm = llm or LlmClient()
        self._notifier = notifier or Notifier(public_url=settings.public_url)
        self._clock = clock
        self._heal_dir = heal_dir if heal_dir is not None else Path(settings.data_dir) / "heal"
        self._tracker = tracker
        self._active_tokens: dict[int, str] = {}
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._spawned: set[asyncio.Task] = set()

    # --- RouterHooks (sync, never raise) ---

    def active(self, cfg: HealerConfig) -> bool:
        return self._settings.healer_enabled and cfg.healer.enabled

    def failure(self, report: FailureReport) -> None:
        try:
            cfg = self._config.get()
            pol = cfg.for_domain(report.domain)
            trace_id = ""
            if self._traces.should_save(report.domain, cfg.healer.trace_min_interval_s):
                trace_id = (
                    self._traces.save(
                        kind="failure",
                        domain=report.domain,
                        url=report.url,
                        result=report.result,
                        failure=report.kind,
                        detail=report.detail,
                        plugin=report.plugin,
                    )
                    or ""
                )
                self._traces.prune(report.domain, pol.trace_keep)
            if self.active(cfg) and pol.enabled and healable(report):
                self._store.note_failure(
                    report.domain,
                    kind=report.kind,
                    url=report.url,
                    trace_id=trace_id,
                    plugin=report.plugin,
                    first_delay_s=cfg.healer.first_delay_s,
                )
                if self._wake is not None:
                    self._wake.set()
        except Exception:
            log.exception("healer failure hook failed")

    def success(self, domain: str) -> None:
        try:
            for iid in self._store.resolve_domain(domain, "recovered: a request passed the challenge"):
                log.info("incident #%d for %s solved: a request passed the challenge", iid, domain)
        except Exception:
            log.exception("healer success hook failed")

    def rollback(self, event: RollbackEvent) -> None:
        try:
            to = f"v{event.to_version}" if event.to_version else "_default"
            notice = Notice(
                "rollback",
                event.scope,
                f"{event.scope}@v{event.from_version} -> {to} after {event.rate:.0%} of {event.samples} runs",
                incident_id=event.incident_id,
            )
            self._spawn(self._notify(notice, self._config.get()))
        except Exception:
            log.exception("healer rollback hook failed")

    def _spawn(self, coro: Coroutine) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            log.warning("no running loop; notification dropped")
            return
        self._spawned.add(task)
        task.add_done_callback(self._spawned.discard)

    # --- lifecycle ---

    def start(self) -> None:
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="healer")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._spawned) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self._llm.aclose()
        await self._notifier.aclose()

    async def _run(self) -> None:
        while True:
            try:
                worked = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("healer tick failed")
                worked = False
            if not worked:
                assert self._wake is not None
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self._config.get().healer.poll_s)
                self._wake.clear()

    # --- one unit of work ---

    async def tick(self) -> bool:
        cfg = self._config.get()
        if not self.active(cfg):
            return False
        now = self._clock()
        acted = False
        for inc in self._store.expired(("escalated",), now):
            await self._to_manual(inc, cfg, "no agent picked it up")
            acted = True
        for inc in self._store.expired(("manual",), now):
            self._store.update_incident(inc["id"], state="gave_up", next_attempt_at=None, note="no manual solve in time")
            await self._notify(
                Notice("gave_up", inc["domain"], "No manual solve in time; giving up.", inc["id"], inc["kind"], inc["plugin"]),
                cfg,
            )
            acted = True
        inc = self._store.due_incident(now)
        if inc is None:
            return acted
        await self._work(inc, cfg)
        return True

    async def _work(self, inc: dict, cfg: HealerConfig) -> None:
        iid, domain = inc["id"], inc["domain"]
        pol = cfg.for_domain(domain)
        if not pol.enabled:
            self._store.update_incident(iid, state="gave_up", next_attempt_at=None, note="healing is off for this domain")
            return
        self._store.update_incident(iid, state="healing")
        token = (
            self._tracker.start("healer", domain, inc["url"], description=f"incident #{iid}: auto attempts", incident_id=iid)
            if self._tracker is not None
            else None
        )
        if token:
            self._active_tokens[iid] = token
        try:
            if inc["tier"] == 0:
                if await self._auto_attempts(inc, pol, cfg):
                    return
                self._store.update_incident(iid, tier=1)
                inc = self._store.incident(iid) or inc
            await self._heal_window(inc, pol, cfg)
        finally:
            if token:
                self._active_tokens.pop(iid, None)
                if self._tracker is not None:
                    self._tracker.finish(token)

    def _replay_ms(self, cfg: HealerConfig) -> int:
        return int(cfg.healer.replay_timeout_s * 1000)

    async def _auto_attempts(self, inc: dict, pol: DomainPolicy, cfg: HealerConfig) -> bool:
        iid, domain = inc["id"], inc["domain"]
        active = self._store.resolve(domain)
        for i in range(pol.escalation.auto_attempts):
            if i == 0:
                spec, src = active.spec(), None
                label = "retry"
            else:
                src = prompt.switch_backend(active.source)
                if src is None:
                    continue
                spec = PluginSpec(scope=domain, version=0, source=src, sha256=sha256(src.encode()).hexdigest())
                label = "switch"
            if self._tracker is not None and iid in self._active_tokens:
                self._tracker.update(self._active_tokens[iid], f"incident #{iid}: auto attempt ({label})")
            out = await self._router.replay(inc["url"], spec, max_timeout_ms=self._replay_ms(cfg))
            trace_id = ""
            if not out.ok:
                trace_id = (
                    self._traces.save(
                        kind="replay",
                        domain=domain,
                        url=inc["url"],
                        result=out.result,
                        failure=out.failure or "",
                        detail=out.message,
                        plugin=active.ref,
                        incident_id=iid,
                    )
                    or ""
                )
            self._store.add_attempt(
                iid, tier=0, result=f"{label}_{'passed' if out.ok else 'failed'}", detail=out.message, trace_id=trace_id
            )
            if not out.ok:
                continue
            if src is None:
                self._store.update_incident(
                    iid, state="solved", next_attempt_at=None, note=f"transient: a replay of {active.ref} passed"
                )
                return True
            rec = self._store.submit(
                domain, src, author="llm", parent=active.version if active.scope == domain else None
            )
            self._store.promote(domain, rec.version)
            note = f"backend switch healed it: {rec.ref}"
            self._store.update_incident(iid, state="solved", next_attempt_at=None, note=note)
            await self._notify(Notice("healed", domain, note, iid, inc["kind"], rec.ref), cfg)
            return True
        return False

    async def _heal_window(self, inc: dict, pol: DomainPolicy, cfg: HealerConfig) -> None:
        iid, domain = inc["id"], inc["domain"]
        ep = await self._llm.pick(pol.llm_order, cfg.llm.profiles)
        if ep is None:
            self._store.update_incident(iid, state="open", next_attempt_at=self._clock() + cfg.healer.llm_wait_s)
            log.info("incident #%d: no LLM endpoint reachable, looking again in %.0fs", iid, cfg.healer.llm_wait_s)
            return
        day = time.strftime("%Y-%m-%d", time.localtime(self._clock()))
        for rnd in range(pol.escalation.heal_rounds):
            if self._store.day_rounds(domain, day) >= pol.daily_rounds:
                self._store.update_incident(
                    iid, state="open", next_attempt_at=_next_midnight(self._clock()), note="daily LLM round cap reached"
                )
                return
            cur = self._store.incident(iid)
            if cur is None or cur["state"] not in ("open", "healing"):
                return
            verdict = await self._round(cur, ep, rnd, cfg)
            if verdict == "wait":
                self._store.update_incident(iid, state="open", next_attempt_at=self._clock() + cfg.healer.llm_wait_s)
                return
            self._store.add_day_round(domain, day)
            if verdict == "passed":
                return
        cur = self._store.incident(iid)
        if cur is None or cur["state"] not in ("open", "healing"):
            return
        w = cur["heal_window"] + 1
        windows = pol.escalation.heal_windows
        if w <= len(windows):
            self._store.update_incident(iid, state="open", heal_window=w, next_attempt_at=self._clock() + windows[w - 1])
        elif pol.escalation.agent_tier:
            self._store.update_incident(iid, state="escalated", tier=2, next_attempt_at=self._clock() + pol.escalation.agent_wait)
            await self._notify(
                Notice(
                    "agent_wanted", domain,
                    f"{w} heal windows failed; waiting {pol.escalation.agent_wait / 3600:g} h for an agent (MCP) "
                    "before a manual solve.",
                    iid, cur["kind"], cur["plugin"],
                ),
                cfg,
            )
        else:
            await self._to_manual(cur, cfg, f"{w} heal windows failed")

    async def _round(self, inc: dict, ep: Endpoint, rnd: int, cfg: HealerConfig) -> str:
        iid, domain = inc["id"], inc["domain"]
        active = self._store.resolve(domain)
        trace = self._traces.load(inc["trace_id"]) if inc["trace_id"] else None
        trace = trace or self._traces.latest(domain, "failure")
        demo = self._traces.latest(domain, "demo")
        all_attempts = self._store.attempts(iid)
        history = [a for a in all_attempts if a["tier"] == 1]
        newest = next((a for a in reversed(history) if a["version"] is not None), None)
        if newest is not None:
            try:
                newest["source"] = self._store.get(newest["scope"], newest["version"]).source
            except StoreError:
                pass
        messages = prompt.build_messages(
            domain=domain,
            url=inc["url"],
            kind=inc["kind"],
            detail=inc["reason"],
            plugin_ref=active.ref,
            plugin_source=active.source,
            trace=trace,
            history=history,
            demo=demo,
            profile=ep.profile,
            load_trace=self._traces.load,
        )
        n = len(all_attempts) + 1
        common = {"tier": 1, "heal_window": inc["heal_window"], "round": rnd, "profile": ep.name, "model": ep.model}
        if self._tracker is not None and iid in self._active_tokens:
            self._tracker.update(self._active_tokens[iid], f"incident #{iid}: round {rnd} via {ep.profile}")
        try:
            reply = await self._llm.complete(ep, messages)
        except LlmUnavailable as e:
            self._store.add_attempt(iid, result="llm_error", detail=str(e), **common)
            return "wait"
        prompt_ref, response_ref = self._save_transcript(iid, n, messages, reply.text)
        refs = {"prompt_ref": prompt_ref, "response_ref": response_ref, "elapsed_s": reply.elapsed_s, **common}
        try:
            source = prompt.extract_plugin(reply.text)
        except prompt.PromptError as e:
            self._store.add_attempt(iid, result="parse", detail=str(e), **refs)
            return "failed"
        try:
            rec = self._store.submit(
                domain, source, author="llm", parent=active.version if active.scope == domain else None
            )
        except GuardError as e:
            self._store.add_attempt(iid, result="guard", detail=str(e), **refs)
            return "failed"
        if self._tracker is not None and iid in self._active_tokens:
            self._tracker.update(self._active_tokens[iid], f"incident #{iid}: replaying candidate round {rnd}")
        out = await self._router.replay(inc["url"], rec.spec(), max_timeout_ms=self._replay_ms(cfg))
        if out.failure == "network":
            self._store.add_attempt(
                iid, result="replay_error", scope=domain, version=rec.version, detail=out.message, **refs
            )
            return "wait"
        if out.ok:
            self._store.promote(domain, rec.version)
            self._store.add_attempt(iid, result="passed", scope=domain, version=rec.version, detail=out.message, **refs)
            self._store.update_incident(
                iid, state="solved", next_attempt_at=None, note=f"healed by {ep.name} ({ep.model}): {rec.ref}"
            )
            await self._notify(
                Notice(
                    "healed", domain, f"{rec.ref} passed the replay after {n} attempt(s)", iid, inc["kind"], rec.ref,
                ),
                cfg,
            )
            await self._cross(rec, cfg)
            return "passed"
        rtrace = (
            self._traces.save(
                kind="replay",
                domain=domain,
                url=inc["url"],
                result=out.result,
                failure=out.failure or "",
                detail=out.message,
                plugin=rec.ref,
                incident_id=iid,
            )
            or ""
        )
        self._store.reject(domain, rec.version)
        self._store.add_attempt(
            iid, result="replay_failed", scope=domain, version=rec.version,
            detail=f"{out.failure}: {out.message}", trace_id=rtrace, **refs,
        )
        return "failed"

    async def _cross(self, rec: PluginRecord, cfg: HealerConfig, author: Author = "llm", tier: int = 1) -> None:
        others = [o for o in self._store.open_incidents() if o["domain"] != rec.scope]
        passed = [rec.scope]
        for o in others[: cfg.healer.cross_replay_max]:
            try:
                spec = PluginSpec(scope=o["domain"], version=0, source=rec.source, sha256=rec.sha256)
                out = await self._router.replay(o["url"], spec, max_timeout_ms=self._replay_ms(cfg))
                if not out.ok:
                    continue
                r2 = self._store.submit(
                    o["domain"], rec.source, author=author, notes=f"from {rec.ref} (cross-incident replay)"
                )
                self._store.promote(o["domain"], r2.version)
                self._store.add_attempt(
                    o["id"], tier=tier, result="passed", scope=o["domain"], version=r2.version,
                    detail=f"cross replay of {rec.ref}",
                )
                self._store.update_incident(
                    o["id"], state="solved", next_attempt_at=None, note=f"healed by a cross replay of {rec.ref}"
                )
                await self._notify(
                    Notice("healed", o["domain"], f"{r2.ref} (copy of {rec.ref}) passed the replay", o["id"], o["kind"], r2.ref),
                    cfg,
                )
                passed.append(o["domain"])
            except Exception:
                log.exception("cross replay of %s on %s failed", rec.ref, o["domain"])
        if len(passed) >= 2 and not any(r.sha256 == rec.sha256 for r in self._store.list("_default")):
            prop = self._store.submit(
                "_default", rec.source, author=author, parent="active", notes=f"proposed _default: passed {', '.join(passed)}"
            )
            await self._notify(
                Notice(
                    "default_proposed", "_default", f"{prop.ref} passed on {', '.join(passed)}; review it before promoting.",
                    plugin=prop.ref,
                ),
                cfg,
            )

    async def agent_healed(self, iid: int, rec: PluginRecord) -> None:
        """An agent's plugin (tier 2, MCP) passed the gate: the same follow-ups as an LLM fix."""
        cfg = self._config.get()
        inc = self._store.incident(iid) or {}
        await self._notify(
            Notice("healed", rec.scope, f"{rec.ref} from an agent (MCP) passed the replay", iid, inc.get("kind", ""), rec.ref),
            cfg,
        )
        await self._cross(rec, cfg, author="agent", tier=2)

    async def _to_manual(self, inc: dict | None, cfg: HealerConfig, why: str) -> None:
        if inc is None:
            return
        iid, domain = inc["id"], inc["domain"]
        self._store.update_incident(
            iid, state="manual", tier=3, next_attempt_at=self._clock() + cfg.healer.give_up_after, note=why
        )
        trace = self._traces.load(inc["trace_id"]) if inc["trace_id"] else None
        await self._notify(
            Notice(
                "manual", domain, f"{why}. Solve it from the dashboard.", iid, inc["kind"], inc["plugin"],
                screenshot=trace.screenshot if trace else None,
            ),
            cfg,
            cfg.for_domain(domain).escalation.manual.notify,
        )

    async def _notify(self, notice: Notice, cfg: HealerConfig, channels: Iterable[str] = _ALL_CHANNELS) -> None:
        event: Event = notice.event
        try:
            await self._notifier.send(cfg.notify, notice, channels)
        except Exception:
            log.exception("notification %s failed", event)

    def _save_transcript(self, iid: int, n: int, messages: list[dict], text: str) -> tuple[str, str]:
        safe = []
        for m in messages:
            c = m["content"]
            if isinstance(c, list):
                c = [
                    {**p, "image_url": {"url": "<screenshot omitted>"}} if p.get("type") == "image_url" else p
                    for p in c
                ]
            safe.append({**m, "content": c})
        d = self._heal_dir / str(iid)
        pref, rref = f"heal/{iid}/a{n}-prompt.json", f"heal/{iid}/a{n}-response.md"
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / f"a{n}-prompt.json").write_text(json.dumps(safe, indent=1), encoding="utf-8")
            (d / f"a{n}-response.md").write_text(text, encoding="utf-8")
        except OSError as e:
            log.warning("saving the heal transcript failed: %s", e)
            return "", ""
        return pref, rref
