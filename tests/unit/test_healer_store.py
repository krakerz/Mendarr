import sqlite3

import pytest

from mendarr.plugins.store import PluginStore

M2_INCIDENTS = (
    "CREATE TABLE incidents (id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL, opened REAL NOT NULL, "
    "state TEXT NOT NULL DEFAULT 'open', tier INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL, "
    "attempts INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', plugin TEXT NOT NULL DEFAULT '')"
)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = PluginStore.open(tmp_path, clock=clock)
    yield s
    s.close()


def fail(store, domain="example.com", trace_id="t1", **kw):
    return store.note_failure(
        domain, kind="challenge_loop", url=f"http://{domain}/x", trace_id=trace_id, plugin="p", **kw
    )


def test_m2_database_gains_columns(tmp_path):
    db = sqlite3.connect(tmp_path / "store.db")
    db.execute(M2_INCIDENTS)
    db.execute("INSERT INTO incidents (domain, opened, reason) VALUES ('old.com', 5.0, 'rollback')")
    db.commit()
    db.close()
    s = PluginStore.open(tmp_path)
    try:
        row = s.incidents()[0]
        assert row["domain"] == "old.com" and row["reason"] == "rollback"
        assert row["failures"] == 0 and row["url"] == "" and row["closed"] is None
    finally:
        s.close()


def test_note_failure_opens_then_refreshes(store, clock):
    iid = fail(store, first_delay_s=30)
    row = store.incident(iid)
    assert row["state"] == "open" and row["failures"] == 1 and row["next_attempt_at"] == clock.t + 30
    assert row["reason"] == "challenge_loop failure"
    clock.t += 5
    again = store.note_failure("example.com", kind="unknown", url="http://example.com/y", trace_id="", plugin="q")
    assert again == iid
    row = store.incident(iid)
    assert row["failures"] == 2 and row["url"].endswith("/y") and row["kind"] == "unknown"
    assert row["trace_id"] == "t1" and row["next_attempt_at"] == 1030.0
    store.note_failure("example.com", kind="unknown", url="http://example.com/y", trace_id="t2", plugin="q")
    assert store.incident(iid)["trace_id"] == "t2"


def test_closed_incident_is_not_refreshed(store, clock):
    iid = fail(store)
    clock.t += 1
    store.update_incident(iid, state="solved")
    assert store.incident(iid)["closed"] == clock.t
    assert fail(store) != iid


def test_due_ignores_urlless_until_failure(store, clock):
    iid = store.open_incident("example.com", "rollback")
    assert store.due_incident(clock.t) is None
    fail(store)
    assert store.due_incident(clock.t)["id"] == iid
    assert store.incident(iid)["next_attempt_at"] == clock.t


def test_due_ordering_and_expired(store, clock):
    a = fail(store, "a.com", first_delay_s=50)
    b = fail(store, "b.com", first_delay_s=10)
    assert store.due_incident(clock.t) is None
    assert store.due_incident(clock.t + 10)["id"] == b
    assert store.due_incident(clock.t + 60)["id"] == b
    store.update_incident(b, state="manual")
    assert store.due_incident(clock.t + 60)["id"] == a
    assert [r["id"] for r in store.expired(("manual",), clock.t + 10)] == [b]
    assert store.expired(("manual",), clock.t) == []
    assert {r["id"] for r in store.open_incidents()} == {a, b}


def test_update_incident_rejects_unknown(store):
    iid = fail(store)
    with pytest.raises(ValueError):
        store.update_incident(iid, bogus=1)


def test_resolve_domain(store, clock):
    iid = fail(store)
    other = fail(store, "other.com")
    assert store.resolve_domain("example.com", "recovered") == [iid]
    row = store.incident(iid)
    assert row["state"] == "solved" and row["note"] == "recovered" and row["next_attempt_at"] is None
    assert row["closed"] == clock.t
    assert store.resolve_domain("example.com", "again") == []
    assert store.incident(other)["state"] == "open"


def test_attempts_and_day_rounds(store):
    iid = fail(store)
    store.add_attempt(iid, tier=1, result="parse", round=1, detail="d")
    store.add_attempt(iid, tier=1, result="guard", round=2, version=3)
    assert store.incident(iid)["attempts"] == 2
    assert [a["result"] for a in store.attempts(iid)] == ["parse", "guard"]
    assert store.day_rounds("a.com", "2026-10-31") == 0
    assert store.add_day_round("a.com", "2026-10-31") == 1
    assert store.add_day_round("a.com", "2026-10-31") == 2
    assert store.add_day_round("a.com", "2026-11-01") == 1
    assert store.add_day_round("b.com", "2026-10-31") == 1
    assert store.day_rounds("a.com", "2026-10-31") == 2
