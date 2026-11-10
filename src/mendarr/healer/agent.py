"""Tier 2: the gate external agents (MCP) go through — guard → replay → core judge → promote, author `agent`."""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urlsplit

from mendarr.core.domains import registrable_domain
from mendarr.core.router import Outcome, Router
from mendarr.core.traces import Trace, TraceStore
from mendarr.healer.config import ConfigFile
from mendarr.healer.loop import Healer
from mendarr.plugins import guard
from mendarr.plugins.guard import GuardError
from mendarr.plugins.store import DEFAULT_SCOPE, OPEN_INCIDENTS, PluginStore, StoreError, normalize_scope
from mendarr.rpc import PluginSpec

log = logging.getLogger(__name__)

PROFILE = "mcp"  # attempts.profile of tier-2 rows; attempts.model holds the client id


class AgentError(Exception):
    """A request the agent has to change; MCP shows it as a tool error."""


class RateLimited(AgentError):
    pass


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


class RateLimiter:
    """Replays per client (token) in a sliding one-hour window; 0 = unlimited."""

    def __init__(self, per_hour: int, clock: Callable[[], float] = time.time) -> None:
        self._per_hour = per_hour
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def take(self, client: str) -> None:
        if self._per_hour <= 0:
            return
        now = self._clock()
        q = self._hits.setdefault(client, deque())
        while q and q[0] <= now - 3600:
            q.popleft()
        if len(q) >= self._per_hour:
            raise RateLimited(
                f"rate limit: {self._per_hour} replays per hour; the next one is free in {int(q[0] + 3600 - now) + 1}s"
            )
        q.append(now)


@dataclass
class ReplayReport:
    ok: bool
    result: str  # passed | replay_failed | replay_error | guard
    message: str
    failure: str | None = None
    final_url: str = ""
    status: int | None = None
    title: str = ""
    challenge_seen: bool = False
    clicks: int = 0
    cookies: list[str] = field(default_factory=list)
    trace_id: str = ""
    steps: list[str] = field(default_factory=list)
    plugin: str = ""
    incident_id: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _report(out: Outcome, result: str, trace_id: str, plugin: str, incident_id: int | None) -> ReplayReport:
    res = out.result
    snap = res.snapshot if res else None
    data = res.trace if res else None
    return ReplayReport(
        ok=out.ok,
        result=result,
        message=out.message,
        failure=out.failure,
        final_url=snap.url if snap else out.url,
        status=snap.status if snap else None,
        title=snap.title if snap else "",
        challenge_seen=bool(res and res.challenge_seen),
        clicks=res.clicks if res else 0,
        cookies=[c.name for c in (snap.cookies if snap else out.cookies)],
        trace_id=trace_id,
        steps=list(data.steps[-40:]) if data is not None and data.steps else [],
        plugin=plugin,
        incident_id=incident_id,
    )


class AgentGate:
    def __init__(
        self,
        store: PluginStore,
        router: Router,
        traces: TraceStore,
        config: ConfigFile,
        healer: Healer | None = None,
        *,
        replays_per_hour: int = 30,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._router = router
        self._traces = traces
        self._config = config
        self._healer = healer
        self._limiter = RateLimiter(replays_per_hour, clock)
        self._replays = asyncio.Semaphore(1)  # one agent replay at a time: keep a worker slot for /v1
        self._locks: dict[str, asyncio.Lock] = {}

    # --- read ---

    @staticmethod
    def _view(r: dict) -> dict:
        return {
            "id": r["id"], "domain": r["domain"], "state": r["state"], "tier": r["tier"], "kind": r["kind"],
            "url": r["url"], "reason": r["reason"], "note": r["note"], "plugin": r["plugin"],
            "failures": r["failures"], "attempts": r["attempts"], "trace_id": r["trace_id"],
            "opened": iso(r["opened"]), "updated": iso(r["updated"]), "next_step_at": iso(r["next_attempt_at"]),
        }

    def incidents(self, include_closed: bool = False) -> list[dict]:
        rows = self._store.incidents()
        rows = rows[:50] if include_closed else [r for r in rows if r["state"] in OPEN_INCIDENTS]
        return [self._view(r) for r in rows]

    def incident(self, iid: int) -> dict:
        r = self._store.incident(iid)
        if r is None:
            raise AgentError(f"no incident #{iid}")
        out = self._view(r)
        out["active_plugin"] = self._store.resolve(r["domain"]).ref
        out["attempt_log"] = [
            {
                "at": iso(a["at"]), "tier": a["tier"], "result": a["result"], "detail": str(a["detail"])[:1000],
                "plugin": f"{a['scope']}@v{a['version']}" if a["version"] is not None else "",
                "trace_id": a["trace_id"], "by": a["model"] or a["profile"],
            }
            for a in self._store.attempts(iid)[-30:]
        ]
        return out

    def trace(self, trace_id: str) -> Trace:
        t = self._traces.load(trace_id.strip())
        if t is None:
            raise AgentError(f"no trace {trace_id!r}; trace ids come from list_incidents, get_incident and replays")
        return t

    def plugin(self, scope: str, version: int | None = None) -> dict:
        try:
            s = normalize_scope(scope)
            rec = self._store.get(s, version) if version is not None else self._store.resolve(s)
            versions = self._store.list(s)
        except StoreError as e:
            raise AgentError(str(e)) from e
        return {
            "ref": rec.ref, "requested": s, "author": rec.author, "status": rec.status, "backend": rec.backend,
            "notes": rec.notes, "created": iso(rec.created), "source": rec.source,
            "versions": [
                {"ref": v.ref, "status": v.status, "author": v.author, "notes": v.notes, "ok": v.ok, "fail": v.fail}
                for v in versions
            ],
        }

    # --- replay ---

    def _replay_ms(self) -> int:
        return int(self._config.get().healer.replay_timeout_s * 1000)

    async def _replay(self, url: str, spec: PluginSpec) -> Outcome:
        async with self._replays:
            return await self._router.replay(url, spec, max_timeout_ms=self._replay_ms())

    def _save_trace(self, domain: str, url: str, out: Outcome, plugin: str, iid: int | None) -> str:
        tid = (
            self._traces.save(
                kind="replay", domain=domain, url=url, result=out.result, failure=out.failure or "",
                detail=out.message, plugin=plugin, incident_id=iid,
            )
            or ""
        )
        self._traces.prune(domain, self._config.get().for_domain(domain).trace_keep)
        return tid

    def _open_incident(self, domain: str) -> dict | None:
        rows = [i for i in self._store.open_incidents() if i["domain"] == domain]
        return max(rows, key=lambda i: i["id"]) if rows else None

    async def test(self, source: str, url: str, client: str) -> ReplayReport:
        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise AgentError("url must be an absolute http(s) URL")
        domain = registrable_domain(url)
        inc = self._open_incident(domain)
        iid = inc["id"] if inc else None
        try:
            guard.check(source)
        except GuardError as e:
            return ReplayReport(ok=False, result="guard", message=str(e), incident_id=iid)
        self._limiter.take(client)
        spec = PluginSpec(scope=domain, version=0, source=source, sha256=sha256(source.encode()).hexdigest())
        out = await self._replay(url, spec)
        log.info("agent %s tested a plugin on %s: %s", client, domain, "passed" if out.ok else out.failure)
        if out.ok:
            return _report(out, "passed", "", "test", iid)
        result = "replay_error" if out.failure == "network" else "replay_failed"
        return _report(out, result, self._save_trace(domain, url, out, "agent test", iid), "test", iid)

    async def submit(self, source: str, scope: str, client: str, notes: str | None = None) -> ReplayReport:
        try:
            domain = normalize_scope(scope)
        except StoreError as e:
            raise AgentError(str(e)) from e
        if domain == DEFAULT_SCOPE:
            raise AgentError("agents submit per-domain plugins only; a new _default needs a human (dashboard or CLI)")
        async with self._locks.setdefault(domain, asyncio.Lock()):
            inc = self._open_incident(domain)
            if inc is None:
                raise AgentError(
                    f"no open incident for {domain}, so there is no failing URL to replay; "
                    "use test_plugin to try a plugin on a URL"
                )
            iid = inc["id"]
            common = {"tier": 2, "profile": PROFILE, "model": client}
            try:
                guard.check(source)
            except GuardError as e:
                self._store.add_attempt(iid, result="guard", detail=str(e), **common)
                return ReplayReport(ok=False, result="guard", message=str(e), incident_id=iid)
            self._limiter.take(client)
            active = self._store.resolve(domain)
            rec = self._store.submit(
                domain, source, author="agent", parent=active.version if active.scope == domain else None, notes=notes
            )
            out = await self._replay(inc["url"], rec.spec())
            if out.failure == "network":
                self._store.add_attempt(
                    iid, result="replay_error", scope=domain, version=rec.version, detail=out.message, **common
                )
                return _report(out, "replay_error", "", rec.ref, iid)
            if out.ok:
                self._store.promote(domain, rec.version)
                self._store.add_attempt(
                    iid, result="passed", scope=domain, version=rec.version, detail=out.message, **common
                )
                note = f"healed by an agent via MCP ({client}): {rec.ref}"
                self._store.update_incident(iid, state="solved", next_attempt_at=None, note=note)
                self._store.resolve_domain(domain, note)
                log.info("incident #%d for %s healed by agent %s: %s", iid, domain, client, rec.ref)
                if self._healer is not None:
                    await self._healer.agent_healed(iid, rec)
                return _report(out, "passed", "", rec.ref, iid)
            tid = self._save_trace(domain, inc["url"], out, rec.ref, iid)
            self._store.reject(domain, rec.version)
            self._store.add_attempt(
                iid, result="replay_failed", scope=domain, version=rec.version,
                detail=f"{out.failure}: {out.message}", trace_id=tid, **common,
            )
            return _report(out, "replay_failed", tid, rec.ref, iid)
