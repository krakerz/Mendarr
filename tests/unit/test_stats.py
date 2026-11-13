import sqlite3
from types import SimpleNamespace

from mendarr.core.stats import CHECK_MISMATCH, DomainStats, StatsHooks


class Rec:
    def __init__(self):
        self.calls = []

    def failure(self, report):
        self.calls.append(("failure", report))

    def success(self, domain):
        self.calls.append(("success", domain))

    def rollback(self, event):
        self.calls.append(("rollback", event))


def test_add_and_summary(tmp_path):
    now = [1000.0]
    s = DomainStats.open(tmp_path / "store.db", clock=lambda: now[0])
    s.add("a.org", True)
    now[0] = 2000.0
    s.add("a.org", False)
    s.add("b.org", True)
    r = s.summary(1500.0)
    assert r["a.org"].ok == 0 and r["a.org"].fail == 1 and r["a.org"].last_at == 2000.0
    assert "b.org" in r
    r = s.summary(0)
    assert (r["a.org"].ok, r["a.org"].fail, r["a.org"].total) == (1, 1, 2)


def test_hooks_delegate_and_skip_network(tmp_path):
    s = DomainStats.open(tmp_path / "store.db")
    inner = Rec()
    h = StatsHooks(s, inner)
    h.success("a.org")
    h.failure(SimpleNamespace(domain="a.org", kind="challenge_loop", background=False))
    h.failure(SimpleNamespace(domain="a.org", kind="network", background=False))
    h.rollback("ev")
    assert [c[0] for c in inner.calls] == ["success", "failure", "failure", "rollback"]
    r = s.summary(0)["a.org"]
    assert (r.ok, r.fail) == (1, 1)


def test_hooks_swallow_db_errors(tmp_path):
    s = DomainStats.open(tmp_path / "store.db")
    s.close()
    inner = Rec()
    StatsHooks(s, inner).success("a.org")
    assert inner.calls == [("success", "a.org")]
    assert issubclass(sqlite3.ProgrammingError, sqlite3.Error)


def test_last_and_counters(tmp_path):
    now = [1000.0]
    s = DomainStats.open(tmp_path / "store.db", clock=lambda: now[0])
    s.set_last("a.org", "solved")
    now[0] = 2000.0
    s.set_last("a.org", "no_challenge")
    assert s.last() == {"a.org": ("no_challenge", 2000.0)}
    s.bump("a.org", CHECK_MISMATCH)
    s.bump("a.org", CHECK_MISMATCH)
    s.bump("b.org", "other")
    assert s.counters(CHECK_MISMATCH) == {"a.org": 2}


def test_background_failure_not_counted_but_forwarded(tmp_path):
    s = DomainStats.open(tmp_path / "store.db")
    inner = Rec()
    report = SimpleNamespace(domain="a.org", kind="timeout", background=True)
    StatsHooks(s, inner).failure(report)
    assert s.summary(0) == {}
    assert inner.calls == [("failure", report)]
