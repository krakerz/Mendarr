import pytest

from mendarr.core.tracker import SolveTracker


def test_tracker_basic_lifecycle():
    now = 1000.0
    tracker = SolveTracker(clock=lambda: now)
    assert tracker.count() == 0
    assert tracker.items() == []

    # Start item
    tok = tracker.start("solve", "example.com", "https://example.com/test", description="plugin: _default@v1")
    assert tracker.count() == 1
    items = tracker.items()
    assert len(items) == 1
    assert items[0]["id"] == tok
    assert items[0]["type"] == "solve"
    assert items[0]["domain"] == "example.com"
    assert items[0]["url"] == "https://example.com/test"
    assert items[0]["description"] == "plugin: _default@v1"
    assert items[0]["elapsed_s"] == 0

    # Advance time & update description
    now = 1015.0
    tracker.update(tok, "plugin: example.com:2")
    items = tracker.items()
    assert items[0]["description"] == "plugin: example.com:2"
    assert items[0]["elapsed_s"] == 15

    # Finish
    tracker.finish(tok)
    assert tracker.count() == 0
    assert tracker.items() == []


def test_tracker_context_manager():
    tracker = SolveTracker()
    assert tracker.count() == 0

    with tracker.track("healer", "target.org", "https://target.org", description="round 1", incident_id=42) as tok:
        assert tracker.count() == 1
        items = tracker.items()
        assert items[0]["id"] == tok
        assert items[0]["type"] == "healer"
        assert items[0]["domain"] == "target.org"
        assert items[0]["incident_id"] == 42

    assert tracker.count() == 0


def test_tracker_context_manager_on_exception():
    tracker = SolveTracker()

    with pytest.raises(ValueError):
        with tracker.track("solve", "boom.org", "https://boom.org"):
            assert tracker.count() == 1
            raise ValueError("kaboom")

    assert tracker.count() == 0
