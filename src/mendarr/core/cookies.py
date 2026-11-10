"""Cleared-cookie cache: SQLite `cookies` table in the data dir's store.db."""

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mendarr.rpc import Cookie

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cookies (
  domain TEXT NOT NULL, egress TEXT NOT NULL, ua TEXT NOT NULL,
  cookies TEXT NOT NULL, solved_at REAL NOT NULL, expires_at REAL NOT NULL,
  PRIMARY KEY (domain, egress, ua))
"""


@dataclass(frozen=True)
class CacheEntry:
    domain: str
    egress: str
    ua: str
    cookies: list[Cookie]
    solved_at: float
    expires_at: float


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def _entry(row: tuple) -> CacheEntry:
    domain, egress, ua, cookies, solved_at, expires_at = row
    return CacheEntry(domain, egress, ua, [Cookie(**c) for c in json.loads(cookies)], solved_at, expires_at)


class CookieCache:
    def __init__(self, conn: sqlite3.Connection, max_ttl_s: float, clock: Callable[[], float]) -> None:
        self._conn = conn
        self._max_ttl_s = max_ttl_s
        self._clock = clock
        self._lock = threading.Lock()

    @classmethod
    def open(
        cls, path: Path, *, max_ttl_s: float, clock: Callable[[], float] = time.time
    ) -> "CookieCache":
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = _connect(str(path))
        except (OSError, sqlite3.Error):
            log.warning("cookie cache: %s not writable, using memory", path)
            conn = _connect(":memory:")
        return cls(conn, max_ttl_s, clock)

    def get(self, domain: str, egress: str) -> CacheEntry | None:
        now = self._clock()
        with self._lock:
            self._conn.execute("DELETE FROM cookies WHERE expires_at <= ?", (now,))
            row = self._conn.execute(
                "SELECT domain, egress, ua, cookies, solved_at, expires_at FROM cookies "
                "WHERE domain = ? AND egress = ? ORDER BY solved_at DESC LIMIT 1",
                (domain, egress),
            ).fetchone()
            self._conn.commit()
        return _entry(row) if row else None

    def put(self, domain: str, egress: str, ua: str, cookies: list[Cookie]) -> CacheEntry | None:
        now = self._clock()
        expires_at = now + self._max_ttl_s
        for c in cookies:
            if c.name == "cf_clearance" and c.expires > 0:
                expires_at = min(expires_at, c.expires)
        if expires_at <= now:
            return None
        entry = CacheEntry(domain, egress, ua, cookies, now, expires_at)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cookies VALUES (?, ?, ?, ?, ?, ?)",
                (domain, egress, ua, json.dumps([c.model_dump() for c in cookies]), now, expires_at),
            )
            self._conn.commit()
        return entry

    def drop(self, domain: str, egress: str, ua: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM cookies WHERE domain = ? AND egress = ? AND ua = ?", (domain, egress, ua)
            )
            self._conn.commit()

    def purge(self, domain: str | None = None) -> int:
        with self._lock:
            if domain is None:
                cur = self._conn.execute("DELETE FROM cookies")
            else:
                cur = self._conn.execute("DELETE FROM cookies WHERE domain = ?", (domain,))
            self._conn.commit()
            return cur.rowcount

    def entries(self) -> list[CacheEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT domain, egress, ua, cookies, solved_at, expires_at FROM cookies ORDER BY solved_at DESC"
            ).fetchall()
        return [_entry(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
