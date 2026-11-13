"""Cleared-cookie cache: SQLite `cookies` table in the data dir's store.db."""

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from mendarr.rpc import Cookie

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cookies (
  domain TEXT NOT NULL, egress TEXT NOT NULL, ua TEXT NOT NULL,
  cookies TEXT NOT NULL, solved_at REAL NOT NULL, expires_at REAL NOT NULL,
  url TEXT NOT NULL DEFAULT '', last_used REAL NOT NULL DEFAULT 0,
  last_check_at REAL NOT NULL DEFAULT 0, last_check_result TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (domain, egress, ua))
"""
# columns added after the first release: ALTER TABLE on open
_ADDED = {
    "url": "TEXT NOT NULL DEFAULT ''",
    "last_used": "REAL NOT NULL DEFAULT 0",
    "last_check_at": "REAL NOT NULL DEFAULT 0",
    "last_check_result": "TEXT NOT NULL DEFAULT ''",
}
_COLS = "domain, egress, ua, cookies, solved_at, expires_at, url, last_used, last_check_at, last_check_result"
SESSION_FALLBACK_S = 24 * 3600.0  # no cap and no cf_clearance expiry: keep the entry this long


@dataclass(frozen=True)
class CacheEntry:
    domain: str
    egress: str
    ua: str
    cookies: list[Cookie]
    solved_at: float
    expires_at: float
    url: str = ""  # last real request URL; "" = not refreshable yet
    last_used: float = 0.0
    last_check_at: float = 0.0  # refresher: when it last looked at this entry (0 = never)
    last_check_result: str = ""


def live(cookies: list[Cookie], now: float) -> list[Cookie]:
    return [c for c in cookies if c.expires <= 0 or c.expires > now]


def lifetime_cookie(entry: CacheEntry) -> str | None:
    """ "cf_clearance" when that cookie's own expiry decided the entry's lifetime, else None (cap or fallback)."""
    for c in entry.cookies:
        if c.name == "cf_clearance" and c.expires > 0 and abs(c.expires - entry.expires_at) < 1:
            return "cf_clearance"
    return None


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(cookies)")}
    for name, decl in _ADDED.items():
        if name not in have:
            conn.execute(f"ALTER TABLE cookies ADD COLUMN {name} {decl}")
            if name == "last_used":
                conn.execute("UPDATE cookies SET last_used = solved_at")


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _entry(row: tuple) -> CacheEntry:
    (
        domain,
        egress,
        ua,
        cookies,
        solved_at,
        expires_at,
        url,
        last_used,
        check_at,
        check_result,
    ) = row
    return CacheEntry(
        domain,
        egress,
        ua,
        [Cookie(**c) for c in json.loads(cookies)],
        solved_at,
        expires_at,
        url,
        last_used,
        check_at,
        check_result,
    )


class CookieCache:
    def __init__(self, conn: sqlite3.Connection, max_ttl_s: float, clock: Callable[[], float]) -> None:
        self._conn = conn
        self._max_ttl_s = max_ttl_s
        self._clock = clock
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: Path, *, max_ttl_s: float, clock: Callable[[], float] = time.time) -> "CookieCache":
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
                f"SELECT {_COLS} FROM cookies WHERE domain = ? AND egress = ? ORDER BY solved_at DESC LIMIT 1",
                (domain, egress),
            ).fetchone()
            self._conn.commit()
        if not row:
            return None
        entry = _entry(row)
        return replace(entry, cookies=live(entry.cookies, now))

    def put(
        self,
        domain: str,
        egress: str,
        ua: str,
        cookies: list[Cookie],
        *,
        url: str | None = None,
        last_used: float | None = None,
    ) -> CacheEntry | None:
        now = self._clock()
        kept = live(cookies, now)
        cap = now + self._max_ttl_s if self._max_ttl_s > 0 else None
        cf = next(
            (c.expires for c in cookies if c.name == "cf_clearance" and c.expires > 0),
            None,
        )
        bounds = [t for t in (cap, cf) if t is not None]
        expires_at = min(bounds) if bounds else now + SESSION_FALLBACK_S
        if expires_at <= now:
            return None
        key = (domain, egress, ua)
        with self._lock:
            self._conn.execute(
                "INSERT INTO cookies (domain, egress, ua, cookies, solved_at, expires_at, url, last_used) "
                "VALUES (:d, :e, :ua, :c, :now, :exp, COALESCE(:url, ''), COALESCE(:lu, :now)) "
                "ON CONFLICT(domain, egress, ua) DO UPDATE SET cookies = :c, solved_at = :now, expires_at = :exp, "
                "url = COALESCE(:url, url), last_used = COALESCE(:lu, last_used)",
                {
                    "d": domain,
                    "e": egress,
                    "ua": ua,
                    "now": now,
                    "exp": expires_at,
                    "url": url,
                    "lu": last_used,
                    "c": json.dumps([c.model_dump() for c in kept]),
                },
            )
            row = self._conn.execute(
                f"SELECT {_COLS} FROM cookies WHERE domain = ? AND egress = ? AND ua = ?",
                key,
            ).fetchone()
            self._conn.commit()
        log.debug(
            "cookie cache put: domain=%s egress=%s lifetime=%s left=%.0fs",
            domain,
            egress,
            "cf_clearance" if cf is not None and expires_at == cf else "cap" if cap else "fallback",
            expires_at - now,
        )
        return _entry(row)

    def touch(self, domain: str, url: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cookies SET url = ?, last_used = ? WHERE domain = ?",
                (url, self._clock(), domain),
            )
            self._conn.commit()

    def set_check(self, domain: str, result: str) -> None:
        """Record the refresher's last check of a domain's entries (all egress/ua rows)."""
        with self._lock:
            self._conn.execute(
                "UPDATE cookies SET last_check_at = ?, last_check_result = ? WHERE domain = ?",
                (self._clock(), result, domain),
            )
            self._conn.commit()

    def drop(self, domain: str, egress: str, ua: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM cookies WHERE domain = ? AND egress = ? AND ua = ?",
                (domain, egress, ua),
            )
            self._conn.commit()
        log.debug("cookie cache drop: domain=%s egress=%s", domain, egress)

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
            rows = self._conn.execute(f"SELECT {_COLS} FROM cookies ORDER BY solved_at DESC").fetchall()
        return [_entry(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
