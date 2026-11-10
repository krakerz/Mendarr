from pathlib import Path

import pytest

from mendarr.core.cookies import CookieCache
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
