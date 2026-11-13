import sqlite3
from pathlib import Path

import pytest

from mendarr.core.cookies import SESSION_FALLBACK_S, CookieCache, lifetime_cookie, live
from mendarr.rpc import Cookie


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cache(tmp_path, clock):
    c = CookieCache.open(tmp_path / "store.db", max_ttl_s=3600, clock=clock)
    yield c
    c.close()


def cf(expires=-1.0):
    return [Cookie(name="cf_clearance", value="x", domain="a.com", expires=expires)]


def test_round_trip(cache):
    entry = cache.put("a.com", "1.1.1.1", "ua1", cf())
    got = cache.get("a.com", "1.1.1.1")
    assert got == entry
    assert got.cookies[0].name == "cf_clearance"
    assert got.expires_at == 1000.0 + 3600


def test_newest_wins_across_uas(cache, clock):
    cache.put("a.com", "e", "ua1", cf())
    clock.now += 10
    cache.put("a.com", "e", "ua2", cf())
    assert cache.get("a.com", "e").ua == "ua2"


def test_expiry_by_max_ttl(cache, clock):
    cache.put("a.com", "e", "ua", cf())
    clock.now += 3601
    assert cache.get("a.com", "e") is None
    assert cache.entries() == []


def test_expiry_by_clearance_earlier_than_max_ttl(cache, clock):
    entry = cache.put("a.com", "e", "ua", cf(expires=1100.0))
    assert entry.expires_at == 1100.0
    clock.now = 1101.0
    assert cache.get("a.com", "e") is None


def test_already_expired_not_stored(cache):
    assert cache.put("a.com", "e", "ua", cf(expires=500.0)) is None
    assert cache.get("a.com", "e") is None


def test_drop(cache):
    cache.put("a.com", "e", "ua", cf())
    cache.drop("a.com", "e", "ua")
    assert cache.get("a.com", "e") is None


def test_purge_domain(cache):
    cache.put("a.com", "e", "ua", cf())
    cache.put("a.com", "e", "ua2", cf())
    cache.put("b.com", "e", "ua", cf())
    assert cache.purge("a.com") == 2
    assert [e.domain for e in cache.entries()] == ["b.com"]
    assert cache.purge() == 1


def test_egress_mismatch(cache):
    cache.put("a.com", "e1", "ua", cf())
    assert cache.get("a.com", "e2") is None


def test_unwritable_path_falls_back_to_memory(clock):
    c = CookieCache.open(Path("/proc/x/store.db"), max_ttl_s=3600, clock=clock)
    c.put("a.com", "e", "ua", cf())
    assert c.get("a.com", "e") is not None
    c.close()


def test_persists_across_reopen(tmp_path, clock):
    p = tmp_path / "store.db"
    c = CookieCache.open(p, max_ttl_s=3600, clock=clock)
    c.put("a.com", "e", "ua", cf())
    c.close()
    c2 = CookieCache.open(p, max_ttl_s=3600, clock=clock)
    assert c2.get("a.com", "e") is not None
    c2.close()


def test_migration_from_old_schema(tmp_path, clock):
    path = tmp_path / "store.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cookies (domain TEXT NOT NULL, egress TEXT NOT NULL, ua TEXT NOT NULL, "
        "cookies TEXT NOT NULL, solved_at REAL NOT NULL, expires_at REAL NOT NULL, PRIMARY KEY (domain, egress, ua))"
    )
    conn.execute("INSERT INTO cookies VALUES ('a.com', '1.1.1.1', 'ua', '[]', 500.0, 9000.0)")
    conn.commit()
    conn.close()
    c = CookieCache.open(path, max_ttl_s=3600, clock=clock)
    e = c.get("a.com", "1.1.1.1")
    assert (e.url, e.last_used, e.last_check_at, e.last_check_result) == (
        "",
        500.0,
        0.0,
        "",
    )
    c.close()


def test_no_cap_follows_clearance(tmp_path, clock):
    c = CookieCache.open(tmp_path / "s.db", max_ttl_s=0, clock=clock)
    e = c.put("a.com", "1.1.1.1", "ua", cf(expires=1000.0 + 120))
    assert e.expires_at == 1120.0 and lifetime_cookie(e) == "cf_clearance"
    c.close()


def test_cap_wins_when_lower(cache):
    e = cache.put("a.com", "1.1.1.1", "ua", cf(expires=1000.0 + 99999))
    assert e.expires_at == 1000.0 + 3600 and lifetime_cookie(e) is None


def test_session_clearance_without_cap_uses_fallback(tmp_path, clock):
    c = CookieCache.open(tmp_path / "s.db", max_ttl_s=0, clock=clock)
    e = c.put("a.com", "1.1.1.1", "ua", cf())
    assert e.expires_at == 1000.0 + SESSION_FALLBACK_S and lifetime_cookie(e) is None
    c.close()


def test_expired_sibling_dropped(cache, clock):
    cookies = cf() + [Cookie(name="short", value="y", domain="a.com", expires=1000.0 + 10)]
    cache.put(
        "a.com",
        "1.1.1.1",
        "ua",
        cookies + [Cookie(name="old", value="z", domain="a.com", expires=500.0)],
    )
    assert {c.name for c in cache.get("a.com", "1.1.1.1").cookies} == {
        "cf_clearance",
        "short",
    }
    clock.now += 20
    assert [c.name for c in cache.get("a.com", "1.1.1.1").cookies] == ["cf_clearance"]
    assert [c.name for c in live(cookies, clock.now)] == ["cf_clearance"]
    assert len(cache.entries()[0].cookies) == 2  # entries() is unfiltered


def test_upsert_keeps_url_and_last_used(cache, clock):
    e = cache.put("a.com", "1.1.1.1", "ua", cf(), url="https://a.com/x", last_used=900.0)
    assert (e.url, e.last_used) == ("https://a.com/x", 900.0)
    clock.now += 5
    e = cache.put("a.com", "1.1.1.1", "ua", cf())
    assert (e.url, e.last_used, e.solved_at) == ("https://a.com/x", 900.0, 1005.0)
    assert len(cache.entries()) == 1
    e = cache.put("a.com", "1.1.1.2", "ua", cf())
    assert (e.url, e.last_used) == ("", 1005.0)


def test_touch_and_set_check(cache, clock):
    cache.put("a.com", "1.1.1.1", "ua", cf())
    clock.now += 7
    cache.touch("a.com", "https://a.com/y")
    cache.set_check("a.com", "ok")
    e = cache.get("a.com", "1.1.1.1")
    assert (e.url, e.last_used, e.last_check_at, e.last_check_result) == (
        "https://a.com/y",
        1007.0,
        1007.0,
        "ok",
    )
    cache.put("a.com", "1.1.1.1", "ua", cf())  # a re-put keeps the check status
    assert cache.get("a.com", "1.1.1.1").last_check_result == "ok"
