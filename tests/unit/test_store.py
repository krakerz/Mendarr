import itertools

import pytest

from mendarr.plugins.guard import GuardError
from mendarr.plugins.store import PluginStore, RollbackPolicy, StoreError

SRC = 'META = {"backend": "camoufox", "notes": "n"}\n\n\nasync def solve(page, ctx):\n    return None\n'
SRC2 = SRC.replace('"n"', '"second"')
PW = SRC.replace("camoufox", "patchright")
POLICY = RollbackPolicy(min_samples=3, window=5)


@pytest.fixture
def store(tmp_path):
    ticks = itertools.count(1000)
    s = PluginStore.open(tmp_path, policy=POLICY, clock=lambda: float(next(ticks)))
    yield s
    s.close()


def _active(store, scope="x.org"):
    rec = store.active(scope)
    return rec.version if rec else None


def _two(store):
    """x.org v1 active, v2 (child) active."""
    store.promote("x.org", store.submit("x.org", SRC).version)
    return store.promote("x.org", store.submit("x.org", SRC2).version)


def test_seed(store, tmp_path):
    rec = store.active("_default")
    assert (rec.version, rec.author, rec.status) == (1, "builtin", "active")
    assert (tmp_path / "plugins" / "_default" / "v1.py").read_text() == rec.source


def test_reopen_no_duplicates_and_sha_drift(store, tmp_path):
    good = store.get("_default", 1).source
    store._conn.execute("UPDATE plugins SET source='x', sha256='y' WHERE scope='_default'")
    store._conn.commit()
    again = PluginStore.open(tmp_path, policy=POLICY)
    assert len(again.list()) == 1
    assert again.get("_default", 1).source == good
    again.close()


def test_submit(store, tmp_path):
    a = store.submit("x.org", SRC)
    assert (a.version, a.parent, a.status, a.backend, a.notes) == (1, None, "candidate", "camoufox", "n")
    store.promote("x.org", 1)
    b = store.submit("X.org", PW, notes="mine")
    assert (b.version, b.parent, b.backend, b.notes) == (2, 1, "patchright", "mine")
    assert (tmp_path / "plugins" / "x.org" / "v2.py").read_text() == PW
    assert store.submit("y.org", SRC).version == 1


def test_submit_guard_violation_stores_nothing(store):
    with pytest.raises(GuardError):
        store.submit("x.org", "import os\n" + SRC)
    assert store.list("x.org") == []


@pytest.mark.parametrize("scope", ["../x", "a/b", "Ex ample", ""])
def test_invalid_scope(store, scope):
    with pytest.raises(StoreError):
        store.submit(scope, SRC)


def test_builtin_author_reserved(store):
    with pytest.raises(StoreError):
        store.submit("x.org", SRC, author="builtin")


def test_resolve(store):
    assert store.resolve("x.org").ref == "_default@v1"
    store.submit("x.org", SRC)
    assert store.resolve("x.org").ref == "_default@v1"
    store.promote("x.org", 1)
    assert store.resolve("x.org").ref == "x.org@v1"
    assert store.resolve("X.ORG").ref == "x.org@v1"


def test_promote_retires_previous_and_reject(store):
    _two(store)
    assert store.get("x.org", 1).status == "retired"
    with pytest.raises(StoreError):
        store.reject("x.org", 2)
    store.reject("x.org", 1)
    with pytest.raises(StoreError):
        store.promote("x.org", 1)
    with pytest.raises(StoreError):
        store.promote("x.org", 9)


def test_rollback_walks_past_rejected(store):
    store.promote("x.org", store.submit("x.org", SRC).version)
    store.promote("x.org", store.submit("x.org", SRC2).version)
    store.promote("x.org", store.submit("x.org", SRC).version)  # v3, parent 2
    store.reject("x.org", 2)
    assert store.rollback("x.org").version == 1


def test_rollback_without_ancestor(store):
    store.promote("x.org", store.submit("x.org", SRC).version)
    assert store.rollback("x.org") is None
    assert store.resolve("x.org").ref == "_default@v1"
    assert store.retire("x.org") is None


def test_builtin_protected(store):
    with pytest.raises(StoreError):
        store.rollback("_default")
    with pytest.raises(StoreError):
        store.retire("_default")
    with pytest.raises(StoreError):
        store.reject("_default", 1)


def test_record_counters_and_auto_rollback(store):
    _two(store)
    assert store.record("x.org", 2, True) is None
    assert store.get("x.org", 2).ok == 1
    assert store.record("x.org", 2, False) is None
    ev = store.record("x.org", 2, False)  # 1/3 < 0.5
    assert ev and (ev.from_version, ev.to_version, ev.samples) == (2, 1, 3)
    assert _active(store) == 1
    (inc,) = store.incidents()
    assert inc["domain"] == "x.org" and inc["plugin"] == "x.org@v2" and inc["state"] == "open"
    assert inc["id"] == ev.incident_id


def test_no_rollback_when_rate_ok_or_disabled(store, tmp_path):
    _two(store)
    store.record("x.org", 2, False)
    store.record("x.org", 2, True)
    assert store.record("x.org", 2, True) is None
    assert _active(store) == 2
    off = PluginStore.open(tmp_path / "off", policy=RollbackPolicy(enabled=False, min_samples=1))
    off.promote("x.org", off.submit("x.org", SRC).version)
    assert off.record("x.org", 1, False) is None
    off.close()


def test_builtin_never_rolls_back(store):
    for _ in range(10):
        assert store.record("_default", 1, False) is None
    assert _active(store, "_default") == 1


def test_window_trimmed(store):
    store.promote("x.org", store.submit("x.org", SRC).version)
    for _ in range(8):
        store.record("x.org", 1, True)
    assert store._conn.execute("SELECT COUNT(*) FROM plugin_runs").fetchone()[0] == 5


def test_reactivated_version_starts_clean_and_incident_refreshed(store):
    _two(store)
    for _ in range(3):
        ev = store.record("x.org", 2, False)
    assert ev and _active(store) == 1
    assert store.record("x.org", 1, False) is None  # window was cleared
    store.promote("x.org", 2)
    for _ in range(3):
        ev2 = store.record("x.org", 2, False)
    assert ev2 and ev2.incident_id == ev.incident_id
    assert len(store.incidents()) == 1


def test_not_writable_dir(tmp_path):
    f = tmp_path / "f"
    f.write_text("")
    s = PluginStore.open(f, policy=POLICY)
    assert s.resolve("x.org").ref == "_default@v1"
    assert s.submit("x.org", SRC).version == 1
    s.close()
    with pytest.raises(StoreError):
        PluginStore.open(f, strict=True)
