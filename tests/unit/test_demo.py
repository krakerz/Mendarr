import pytest

from mendarr.worker import demo as demo_mod
from mendarr.worker.demo import MAX_EVENTS, DemoRecorder


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


class FakeFrameEl:
    async def bounding_box(self):
        return {"x": 10, "y": 20, "width": 300, "height": 65}

    async def evaluate(self, js):
        return "iframe#cf"


class FakeFrame:
    def __init__(self, url, parent=None):
        self.url = url
        self.parent_frame = parent

    async def frame_element(self):
        return FakeFrameEl()


class FakeContext:
    def __init__(self):
        self.binding = None
        self.scripts = []

    async def expose_binding(self, name, cb):
        self.binding = name

    async def add_init_script(self, script):
        self.scripts.append(script)


class FakePage:
    def __init__(self):
        self.handlers = {}

    def on(self, ev, cb):
        self.handlers[ev] = cb


@pytest.mark.asyncio
async def test_events_steps_and_summary():
    main = FakeFrame("https://x.test/")
    child = FakeFrame("https://challenges.test/w", parent=main)
    d = DemoRecorder(clock=FakeClock())
    page = FakePage()
    d.watch(page)
    page.handlers["framenavigated"](main)
    page.handlers["framenavigated"](child)
    page.handlers["framenavigated"](FakeFrame("about:blank"))
    d._on_event({"frame": main}, {"type": "click", "selector": "button#go", "x": 5, "y": 6, "trusted": True})
    d._on_event({"frame": child}, {"type": "click", "selector": "div", "x": 1, "y": 2, "trusted": True})
    for _ in range(5):
        d._on_event({"frame": main}, {"type": "key", "key": "char"})
    d._on_event({"frame": main}, {"type": "key", "key": "Enter"})
    d._on_event({"frame": main}, "junk")
    summary = await d.summary("https://x.test/done", [{"name": "cf_clearance", "value": "SECRET", "domain": "x.test"}])
    assert d.start_url == "https://x.test/"
    assert [e["type"] for e in d.events] == ["nav", "nav", "click", "click", "typing", "key"]
    assert d.events[4]["count"] == 5
    assert summary["frames"] == ["https://challenges.test/w"]
    child_click = d.events[3]
    assert child_click["frame_selector"] == "iframe#cf" and child_click["frame_box"]["width"] == 300
    assert all("value" not in c for c in summary["cookies"])
    assert "SECRET" not in repr(summary)
    steps = "\n".join(d.steps())
    assert "button#go" in steps and "typed 5 characters" in steps and "iframe#cf" in steps


def test_event_cap():
    d = DemoRecorder(clock=FakeClock())
    for _ in range(MAX_EVENTS + 20):
        d._on_event({"frame": FakeFrame("https://x.test/")}, {"type": "submit", "selector": "form"})
    assert len(d.events) == MAX_EVENTS


@pytest.mark.asyncio
async def test_install_substitutes_binding():
    ctx = FakeContext()
    d = DemoRecorder()
    await d.install(ctx)
    assert ctx.binding == d.name
    assert "__BINDING__" not in ctx.scripts[0] and d.name in ctx.scripts[0]
    assert "__BINDING__" in demo_mod.DEMO_JS
