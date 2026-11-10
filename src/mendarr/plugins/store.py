"""Plugin store: SQLite tables beside the cookie cache, sources mirrored to data/plugins/<scope>/v<N>.py."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

from mendarr.config import Settings
from mendarr.plugins import guard
from mendarr.rpc import PluginSpec

log = logging.getLogger(__name__)

DEFAULT_SCOPE = "_default"
Author = Literal["builtin", "llm", "agent", "human"]
Status = Literal["candidate", "active", "retired", "rejected"]
_SCOPE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_CLOSED_INCIDENTS = ("solved", "gave_up")
OPEN_INCIDENTS = ("open", "healing", "escalated", "manual")
# M3 columns, added in place to M2 databases ("window" is an SQL keyword, hence heal_window)
_INCIDENT_EXTRA = (
    ("kind", "TEXT NOT NULL DEFAULT ''"),
    ("url", "TEXT NOT NULL DEFAULT ''"),
    ("trace_id", "TEXT NOT NULL DEFAULT ''"),
    ("heal_window", "INTEGER NOT NULL DEFAULT 0"),
    ("failures", "INTEGER NOT NULL DEFAULT 0"),
    ("updated", "REAL"),
    ("closed", "REAL"),
    ("note", "TEXT NOT NULL DEFAULT ''"),
)
_INCIDENT_FIELDS = frozenset(
    {"state", "tier", "next_attempt_at", "attempts", "reason", "plugin", "kind", "url", "trace_id",
     "heal_window", "failures", "note"}
)
# attempt results: retry_passed retry_failed switch_passed switch_failed llm_error parse guard
# replay_failed replay_error passed
_COLS = "scope, version, source, sha256, parent, author, status, backend, notes, created, ok, fail"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS plugins (
  scope TEXT NOT NULL, version INTEGER NOT NULL, source TEXT NOT NULL, sha256 TEXT NOT NULL,
  parent INTEGER, author TEXT NOT NULL, status TEXT NOT NULL, backend TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
  ok INTEGER NOT NULL DEFAULT 0, fail INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (scope, version));
CREATE UNIQUE INDEX IF NOT EXISTS plugins_one_active ON plugins (scope) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS plugin_runs (
  scope TEXT NOT NULL, version INTEGER NOT NULL, ok INTEGER NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS plugin_runs_by_version ON plugin_runs (scope, version);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL, opened REAL NOT NULL,
  state TEXT NOT NULL DEFAULT 'open', tier INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', plugin TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident INTEGER NOT NULL, at REAL NOT NULL, tier INTEGER NOT NULL,
  heal_window INTEGER NOT NULL DEFAULT 0, round INTEGER NOT NULL DEFAULT 0,
  profile TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '', version INTEGER,
  result TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', trace_id TEXT NOT NULL DEFAULT '',
  prompt_ref TEXT NOT NULL DEFAULT '', response_ref TEXT NOT NULL DEFAULT '', elapsed_s REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS attempts_by_incident ON attempts (incident);
CREATE TABLE IF NOT EXISTS heal_days (
  domain TEXT NOT NULL, day TEXT NOT NULL, rounds INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (domain, day));
"""


class StoreError(Exception):
    pass


@dataclass(frozen=True)
class RollbackPolicy:
    enabled: bool = True
    min_samples: int = 5
    threshold: float = 0.5
    window: int = 20

    @classmethod
    def from_settings(cls, s: Settings) -> "RollbackPolicy":
        return cls(s.rollback_enabled, s.rollback_min_samples, s.rollback_threshold, s.rollback_window)


@dataclass(frozen=True)
class PluginRecord:
    scope: str
    version: int
    source: str
    sha256: str
    parent: int | None
    author: Author
    status: Status
    backend: str
    notes: str
    created: float
    ok: int
    fail: int

    @property
    def ref(self) -> str:
        return f"{self.scope}@v{self.version}"

    def spec(self) -> PluginSpec:
        return PluginSpec(scope=self.scope, version=self.version, source=self.source, sha256=self.sha256)

    def summary(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "source"}


@dataclass(frozen=True)
class RollbackEvent:
    scope: str
    from_version: int
    to_version: int | None  # None: the scope falls back to _default
    rate: float
    samples: int
    incident_id: int


def normalize_scope(scope: str) -> str:
    s = scope.strip().lower()
    if s != DEFAULT_SCOPE and not _SCOPE.match(s):
        raise StoreError(f"invalid scope {scope!r}: use _default or a domain like example.org")
    return s


def _sha(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(incidents)")}
    for name, decl in _INCIDENT_EXTRA:
        if name not in have:
            conn.execute(f"ALTER TABLE incidents ADD COLUMN {name} {decl}")


def _dicts(cur: sqlite3.Cursor) -> list[dict]:
    keys = [d[0] for d in cur.description]
    return [dict(zip(keys, r)) for r in cur.fetchall()]


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


class PluginStore:
    def __init__(
        self, conn: sqlite3.Connection, plugins_dir: Path | None, policy: RollbackPolicy, clock: Callable[[], float]
    ) -> None:
        self._conn = conn
        self._plugins_dir = plugins_dir
        self._policy = policy
        self._clock = clock
        self._lock = threading.RLock()

    @classmethod
    def open(
        cls,
        data_dir: Path,
        *,
        policy: RollbackPolicy = RollbackPolicy(),
        clock: Callable[[], float] = time.time,
        strict: bool = False,
    ) -> "PluginStore":
        plugins_dir: Path | None
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            conn = _connect(str(data_dir / "store.db"))
            plugins_dir = data_dir / "plugins"
        except (OSError, sqlite3.Error) as e:
            if strict:
                raise StoreError(f"plugin store {data_dir} is not writable: {e}") from e
            log.warning("plugin store %s is not writable (%s); using memory only", data_dir, e)
            conn = _connect(":memory:")
            plugins_dir = None
        store = cls(conn, plugins_dir, policy, clock)
        store._seed()
        return store

    def _seed(self) -> None:
        source = resources.files("mendarr.plugins.builtin").joinpath("_default.py").read_text(encoding="utf-8")
        meta = guard.check(source).meta
        sha = _sha(source)
        backend = str(meta["backend"])
        notes = str(meta.get("notes", ""))
        with self._lock:
            row = self._row(DEFAULT_SCOPE, 1)
            if row is None:
                status = "retired" if self._active(DEFAULT_SCOPE) else "active"
                self._conn.execute(
                    f"INSERT OR IGNORE INTO plugins ({_COLS}) VALUES (?,?,?,?,NULL,'builtin',?,?,?,?,0,0)",
                    (DEFAULT_SCOPE, 1, source, sha, status, backend, notes, self._clock()),
                )
            elif row.sha256 != sha:
                self._conn.execute(
                    "UPDATE plugins SET source=?, sha256=?, backend=?, notes=? WHERE scope=? AND version=1",
                    (source, sha, backend, notes, DEFAULT_SCOPE),
                )
            if self._active(DEFAULT_SCOPE) is None:
                self._conn.execute("UPDATE plugins SET status='active' WHERE scope=? AND version=1", (DEFAULT_SCOPE,))
            self._conn.commit()
        self._write_file(DEFAULT_SCOPE, 1, source)

    def _row(self, scope: str, version: int) -> PluginRecord | None:
        r = self._conn.execute(f"SELECT {_COLS} FROM plugins WHERE scope=? AND version=?", (scope, version)).fetchone()
        return PluginRecord(*r) if r else None

    def _active(self, scope: str) -> PluginRecord | None:
        r = self._conn.execute(f"SELECT {_COLS} FROM plugins WHERE scope=? AND status='active'", (scope,)).fetchone()
        return PluginRecord(*r) if r else None

    def _need(self, scope: str, version: int) -> PluginRecord:
        rec = self._row(scope, version)
        if rec is None:
            raise StoreError(f"unknown plugin {scope}@v{version}")
        return rec

    def _set_status(self, scope: str, version: int, status: str) -> None:
        self._conn.execute("UPDATE plugins SET status=? WHERE scope=? AND version=?", (status, scope, version))

    def _clear_runs(self, scope: str, version: int) -> None:
        self._conn.execute("DELETE FROM plugin_runs WHERE scope=? AND version=?", (scope, version))

    def submit(
        self,
        scope: str,
        source: str,
        *,
        author: Author = "human",
        parent: int | Literal["active"] | None = "active",
        notes: str | None = None,
    ) -> PluginRecord:
        scope = normalize_scope(scope)
        if author == "builtin":
            raise StoreError("builtin is reserved")
        report = guard.check(source)
        with self._lock:
            if parent == "active":
                act = self._active(scope)
                parent_v = act.version if act else None
            elif parent is None:
                parent_v = None
            else:
                if self._row(scope, parent) is None:
                    raise StoreError(f"unknown parent {scope}@v{parent}")
                parent_v = parent
            version = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM plugins WHERE scope=?", (scope,)
            ).fetchone()[0]
            self._conn.execute(
                f"INSERT INTO plugins ({_COLS}) VALUES (?,?,?,?,?,?,'candidate',?,?,?,0,0)",
                (
                    scope,
                    version,
                    source,
                    _sha(source),
                    parent_v,
                    author,
                    str(report.meta["backend"]),
                    notes if notes is not None else str(report.meta.get("notes", "")),
                    self._clock(),
                ),
            )
            self._conn.commit()
            self._write_file(scope, version, source)
            return self._need(scope, version)

    def promote(self, scope: str, version: int) -> PluginRecord:
        scope = normalize_scope(scope)
        with self._lock:
            rec = self._need(scope, version)
            if rec.status == "rejected":
                raise StoreError(f"{rec.ref} was rejected")
            if rec.status == "active":
                return rec
            cur = self._active(scope)
            if cur:
                self._set_status(scope, cur.version, "retired")
            self._set_status(scope, version, "active")
            self._clear_runs(scope, version)
            self._conn.commit()
            return self._need(scope, version)

    def rollback(self, scope: str) -> PluginRecord | None:
        scope = normalize_scope(scope)
        with self._lock:
            rec = self._rollback(scope)
            self._conn.commit()
            return rec

    def _rollback(self, scope: str) -> PluginRecord | None:
        cur = self._active(scope)
        if cur is None:
            raise StoreError(f"{scope} has no active plugin")
        if cur.author == "builtin":
            raise StoreError("the builtin can't be rolled back")
        self._set_status(scope, cur.version, "retired")
        p = cur.parent
        while p is not None:
            r = self._row(scope, p)
            if r and r.status != "rejected":
                self._set_status(scope, p, "active")
                self._clear_runs(scope, p)
                return self._need(scope, p)
            p = r.parent if r else None
        if scope == DEFAULT_SCOPE:
            self._set_status(scope, 1, "active")
            self._clear_runs(scope, 1)
            return self._need(scope, 1)
        return None

    def retire(self, scope: str) -> PluginRecord | None:
        scope = normalize_scope(scope)
        if scope == DEFAULT_SCOPE:
            raise StoreError("use rollback for _default")
        with self._lock:
            cur = self._active(scope)
            if cur is None:
                return None
            self._set_status(scope, cur.version, "retired")
            self._conn.commit()
            return self._need(scope, cur.version)

    def reject(self, scope: str, version: int) -> PluginRecord:
        scope = normalize_scope(scope)
        with self._lock:
            rec = self._need(scope, version)
            if rec.author == "builtin":
                raise StoreError("the builtin can't be rejected")
            if rec.status == "active":
                raise StoreError("roll back or retire it first")
            self._set_status(scope, version, "rejected")
            self._conn.commit()
            return self._need(scope, version)

    def get(self, scope: str, version: int) -> PluginRecord:
        with self._lock:
            return self._need(normalize_scope(scope), version)

    def active(self, scope: str) -> PluginRecord | None:
        with self._lock:
            return self._active(normalize_scope(scope))

    def list(self, scope: str | None = None) -> list[PluginRecord]:
        with self._lock:
            if scope is None:
                rows = self._conn.execute(f"SELECT {_COLS} FROM plugins ORDER BY scope, version").fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {_COLS} FROM plugins WHERE scope=? ORDER BY scope, version", (normalize_scope(scope),)
                ).fetchall()
            return [PluginRecord(*r) for r in rows]

    def resolve(self, domain: str) -> PluginRecord:
        d = domain.strip().lower()
        with self._lock:
            rec = self._active(d) if _SCOPE.match(d) else None
            return rec or self._active(DEFAULT_SCOPE) or self._need(DEFAULT_SCOPE, 1)

    def record(self, scope: str, version: int, ok: bool) -> RollbackEvent | None:
        with self._lock:
            rec = self._row(scope, version)
            if rec is None:
                return None
            col = "ok" if ok else "fail"
            self._conn.execute(f"UPDATE plugins SET {col} = {col} + 1 WHERE scope=? AND version=?", (scope, version))
            self._conn.execute(
                "INSERT INTO plugin_runs VALUES (?,?,?,?)", (scope, version, 1 if ok else 0, self._clock())
            )
            keep = max(self._policy.window, self._policy.min_samples)
            self._conn.execute(
                "DELETE FROM plugin_runs WHERE scope=? AND version=? AND rowid NOT IN ("
                "SELECT rowid FROM plugin_runs WHERE scope=? AND version=? ORDER BY rowid DESC LIMIT ?)",
                (scope, version, scope, version, keep),
            )
            event = None
            if self._policy.enabled and rec.status == "active" and rec.author != "builtin":
                n, oks = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(ok), 0) FROM plugin_runs WHERE scope=? AND version=?",
                    (scope, version),
                ).fetchone()
                if n >= self._policy.min_samples and oks / n < self._policy.threshold:
                    new = self._rollback(scope)
                    incident = self._open_incident(
                        scope, f"auto-rollback: {rec.ref} succeeded {oks}/{n} recent runs", rec.ref
                    )
                    event = RollbackEvent(scope, version, new.version if new else None, oks / n, n, incident)
            self._conn.commit()
            return event

    def open_incident(self, domain: str, reason: str, plugin: str = "") -> int:
        with self._lock:
            iid = self._open_incident(domain, reason, plugin)
            self._conn.commit()
            return iid

    def _open_incident(self, domain: str, reason: str, plugin: str) -> int:
        marks = ",".join("?" * len(_CLOSED_INCIDENTS))
        row = self._conn.execute(
            f"SELECT id FROM incidents WHERE domain=? AND state NOT IN ({marks}) ORDER BY id DESC LIMIT 1",
            (domain, *_CLOSED_INCIDENTS),
        ).fetchone()
        if row:
            self._conn.execute("UPDATE incidents SET reason=?, plugin=? WHERE id=?", (reason, plugin, row[0]))
            return row[0]
        cur = self._conn.execute(
            "INSERT INTO incidents (domain, opened, state, reason, plugin) VALUES (?,?,'open',?,?)",
            (domain, self._clock(), reason, plugin),
        )
        return cur.lastrowid

    def incidents(self, state: str | None = None) -> list[dict]:
        with self._lock:
            if state is None:
                return _dicts(self._conn.execute("SELECT * FROM incidents ORDER BY id DESC"))
            return _dicts(self._conn.execute("SELECT * FROM incidents WHERE state=? ORDER BY id DESC", (state,)))

    def incident(self, iid: int) -> dict | None:
        with self._lock:
            rows = _dicts(self._conn.execute("SELECT * FROM incidents WHERE id=?", (iid,)))
        return rows[0] if rows else None

    # --- M3 incident lifecycle ---

    def note_failure(
        self, domain: str, *, kind: str, url: str, trace_id: str, plugin: str, first_delay_s: float = 0.0
    ) -> int:
        marks = ",".join("?" * len(_CLOSED_INCIDENTS))
        now = self._clock()
        with self._lock:
            row = self._conn.execute(
                f"SELECT id FROM incidents WHERE domain=? AND state NOT IN ({marks}) ORDER BY id DESC LIMIT 1",
                (domain, *_CLOSED_INCIDENTS),
            ).fetchone()
            if row:
                iid = row[0]
                self._conn.execute(
                    "UPDATE incidents SET kind=?, url=?, trace_id=CASE WHEN ?='' THEN trace_id ELSE ? END, "
                    "plugin=?, failures=failures+1, updated=?, next_attempt_at=COALESCE(next_attempt_at, ?) "
                    "WHERE id=?",
                    (kind, url, trace_id, trace_id, plugin, now, now + first_delay_s, iid),
                )
            else:
                iid = self._conn.execute(
                    "INSERT INTO incidents (domain, opened, state, reason, plugin, kind, url, trace_id, "
                    "failures, updated, next_attempt_at) VALUES (?,?,'open',?,?,?,?,?,1,?,?)",
                    (domain, now, f"{kind} failure", plugin, kind, url, trace_id, now, now + first_delay_s),
                ).lastrowid
            self._conn.commit()
        return iid

    def due_incident(self, now: float) -> dict | None:
        with self._lock:
            rows = _dicts(
                self._conn.execute(
                    "SELECT * FROM incidents WHERE state IN ('open','healing') AND url != '' "
                    "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                    "ORDER BY COALESCE(next_attempt_at, 0), id LIMIT 1",
                    (now,),
                )
            )
        return rows[0] if rows else None

    def expired(self, states: tuple[str, ...], now: float) -> list[dict]:
        marks = ",".join("?" * len(states))
        with self._lock:
            return _dicts(
                self._conn.execute(
                    f"SELECT * FROM incidents WHERE state IN ({marks}) AND next_attempt_at IS NOT NULL "
                    "AND next_attempt_at <= ? ORDER BY id",
                    (*states, now),
                )
            )

    def open_incidents(self) -> list[dict]:
        marks = ",".join("?" * len(OPEN_INCIDENTS))
        with self._lock:
            return _dicts(
                self._conn.execute(
                    f"SELECT * FROM incidents WHERE state IN ({marks}) AND url != '' ORDER BY id", OPEN_INCIDENTS
                )
            )

    def update_incident(self, iid: int, **fields: object) -> None:
        bad = set(fields) - _INCIDENT_FIELDS
        if bad:
            raise ValueError(f"unknown incident fields: {sorted(bad)}")
        now = self._clock()
        cols = dict(fields, updated=now)
        if fields.get("state") in _CLOSED_INCIDENTS:
            cols["closed"] = now
        sets = ", ".join(f"{k}=?" for k in cols)
        with self._lock:
            self._conn.execute(f"UPDATE incidents SET {sets} WHERE id=?", (*cols.values(), iid))
            self._conn.commit()

    def resolve_domain(self, domain: str, note: str) -> list[int]:
        marks = ",".join("?" * len(_CLOSED_INCIDENTS))
        now = self._clock()
        with self._lock:
            ids = [
                r[0]
                for r in self._conn.execute(
                    f"SELECT id FROM incidents WHERE domain=? AND state NOT IN ({marks}) ORDER BY id",
                    (domain, *_CLOSED_INCIDENTS),
                )
            ]
            for iid in ids:
                self._conn.execute(
                    "UPDATE incidents SET state='solved', note=?, next_attempt_at=NULL, closed=?, updated=? "
                    "WHERE id=?",
                    (note, now, now, iid),
                )
            if ids:
                self._conn.commit()
        return ids

    def add_attempt(
        self,
        incident: int,
        *,
        tier: int,
        result: str,
        heal_window: int = 0,
        round: int = 0,
        profile: str = "",
        model: str = "",
        scope: str = "",
        version: int | None = None,
        detail: str = "",
        trace_id: str = "",
        prompt_ref: str = "",
        response_ref: str = "",
        elapsed_s: float = 0.0,
    ) -> int:
        now = self._clock()
        with self._lock:
            aid = self._conn.execute(
                "INSERT INTO attempts (incident, at, tier, heal_window, round, profile, model, scope, version, "
                "result, detail, trace_id, prompt_ref, response_ref, elapsed_s) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (incident, now, tier, heal_window, round, profile, model, scope, version, result, detail,
                 trace_id, prompt_ref, response_ref, elapsed_s),
            ).lastrowid
            self._conn.execute(
                "UPDATE incidents SET attempts = attempts + 1, updated=? WHERE id=?", (now, incident)
            )
            self._conn.commit()
        return aid

    def attempts(self, incident: int) -> list[dict]:
        with self._lock:
            return _dicts(self._conn.execute("SELECT * FROM attempts WHERE incident=? ORDER BY id", (incident,)))

    def day_rounds(self, domain: str, day: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT rounds FROM heal_days WHERE domain=? AND day=?", (domain, day)
            ).fetchone()
        return row[0] if row else 0

    def add_day_round(self, domain: str, day: str) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO heal_days VALUES (?,?,1) ON CONFLICT(domain, day) DO UPDATE SET rounds = rounds + 1",
                (domain, day),
            )
            self._conn.commit()
            return self._conn.execute(
                "SELECT rounds FROM heal_days WHERE domain=? AND day=?", (domain, day)
            ).fetchone()[0]

    def _write_file(self, scope: str, version: int, source: str) -> None:
        if self._plugins_dir is None:
            return
        try:
            d = self._plugins_dir / scope
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f"v{version}.py.tmp"
            tmp.write_text(source, encoding="utf-8")
            os.replace(tmp, d / f"v{version}.py")
        except OSError as e:
            log.warning("could not mirror %s@v%d to disk: %s", scope, version, e)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
