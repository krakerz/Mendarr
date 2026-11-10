"""Per-domain challenge outcomes for the dashboard: a request passed a challenge, or a solve failed."""

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mendarr.core.router import RouterHooks
    from mendarr.core.traces import FailureReport
    from mendarr.plugins.store import RollbackEvent

log = logging.getLogger(__name__)
KEEP_S = 30 * 86400
_SCHEMA = """
CREATE TABLE IF NOT EXISTS domain_runs (domain TEXT NOT NULL, ok INTEGER NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS domain_runs_by_at ON domain_runs (at);
"""


@dataclass(frozen=True)
class DomainRate:
    ok: int
    fail: int
    last_at: float

    @property
    def total(self) -> int:
        return self.ok + self.fail


class DomainStats:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time) -> None:
        self._conn = conn
        self._clock = clock
        self._lock = threading.Lock()
        self._writes = 0

    @classmethod
    def open(cls, path: Path, clock: Callable[[], float] = time.time) -> "DomainStats":
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        except (OSError, sqlite3.Error):
            log.warning("domain stats: %s not writable, using memory", path)
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.executescript(_SCHEMA)
        conn.commit()
        return cls(conn, clock)

    def add(self, domain: str, ok: bool) -> None:
        now = self._clock()
        with self._lock:
            self._conn.execute("INSERT INTO domain_runs VALUES (?, ?, ?)", (domain, 1 if ok else 0, now))
            self._writes += 1
            if self._writes % 500 == 1:
                self._conn.execute("DELETE FROM domain_runs WHERE at < ?", (now - KEEP_S,))
            self._conn.commit()

    def summary(self, since: float) -> dict[str, DomainRate]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT domain, COALESCE(SUM(ok), 0), COUNT(*) - COALESCE(SUM(ok), 0), MAX(at) "
                "FROM domain_runs WHERE at >= ? GROUP BY domain",
                (since,),
            ).fetchall()
        return {d: DomainRate(ok, fail, last) for d, ok, fail, last in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class StatsHooks:
    """RouterHooks that count outcomes per domain, then hand them to the healer."""

    def __init__(self, stats: DomainStats, inner: "RouterHooks | None" = None) -> None:
        self.stats = stats
        self.inner = inner

    def _add(self, domain: str, ok: bool) -> None:
        try:
            self.stats.add(domain, ok)
        except sqlite3.Error:
            log.exception("domain stats update failed")

    def failure(self, report: "FailureReport") -> None:
        if report.kind != "network":  # site or worker down says nothing about solving
            self._add(report.domain, False)
        if self.inner is not None:
            self.inner.failure(report)

    def success(self, domain: str) -> None:
        self._add(domain, True)
        if self.inner is not None:
            self.inner.success(domain)

    def rollback(self, event: "RollbackEvent") -> None:
        if self.inner is not None:
            self.inner.rollback(event)
