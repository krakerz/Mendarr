import base64
from types import SimpleNamespace

from mendarr.worker.tracing import MAX_CONSOLE, MAX_NETWORK, TraceRecorder


class FakeFrame:
    def __init__(self, url, dom="<p>f</p>", parent=None, fail=False):
        self.url = url
        self.name = "f"
        self.parent_frame = parent
        self._dom = dom
        self._fail = fail

    async def evaluate(self, js, limit):
        if self._fail:
            raise RuntimeError("detached")
        return self._dom


class FakePage:
    def __init__(self, frames=(), shot_fails=False):
        self.handlers: dict = {}
        self.main_frame = FakeFrame("https://example.com/")
        self.frames = [self.main_frame, *frames]
        self._shot_fails = shot_fails

    def on(self, name, cb):
        self.handlers[name] = cb

    async def screenshot(self, **kw):
        if self._shot_fails:
            raise RuntimeError("no shot")
        return b"png"

    async def evaluate(self, js, limit):
        return "<html>page</html>"


def response(url="https://example.com/", status=200, cf=""):
    return SimpleNamespace(
        url=url, status=status, request=SimpleNamespace(resource_type="document"), headers={"cf-mitigated": cf}
    )


def test_console_filters_and_caps():
    page = FakePage()
    rec = TraceRecorder(page)
    page.handlers["console"](SimpleNamespace(type="log", text="ignored"))
    page.handlers["console"](SimpleNamespace(type="error", text="boom"))
    for _ in range(MAX_CONSOLE + 10):
        page.handlers["pageerror"](Exception("x"))
    assert rec.console[0] == "error: boom"
    assert len(rec.console) == MAX_CONSOLE


def test_network_caps_and_failure():
    page = FakePage()
    rec = TraceRecorder(page)
    page.handlers["response"](response(status=403, cf="challenge"))
    assert rec.network[0]["status"] == 403
    assert rec.network[0]["cf"] == "challenge"
    page.handlers["requestfailed"](SimpleNamespace(url="https://x/", resource_type="script", failure="net::ERR"))
    assert rec.network[1]["failure"] == "net::ERR"
    for _ in range(MAX_NETWORK + 10):
        page.handlers["response"](response())
    assert len(rec.network) == MAX_NETWORK


async def test_collect_full():
    sub = FakeFrame("https://challenges.example/", dom="<div>cf</div>", parent=FakePage().main_frame)
    page = FakePage(frames=[sub])
    rec = TraceRecorder(page)
    data = await rec.collect(page, ["a", "b"])
    assert base64.b64decode(data.screenshot_b64) == b"png"
    assert data.dom.startswith("<html>page</html>")
    assert "<!-- frame https://challenges.example/ -->\n<div>cf</div>" in data.dom
    assert data.frames == [{"url": "https://challenges.example/", "name": "f", "parent": "https://example.com/"}]
    assert data.steps == ["a", "b"]


async def test_unreadable_frame():
    page = FakePage(frames=[FakeFrame("https://bad.example/", fail=True)])
    data = await TraceRecorder(page).collect(page, [])
    assert "not readable (RuntimeError)" in data.dom
    assert "<html>page</html>" in data.dom


async def test_screenshot_failure_keeps_dom():
    page = FakePage(shot_fails=True)
    data = await TraceRecorder(page).collect(page, [])
    assert data.screenshot_b64 is None
    assert data.dom == "<html>page</html>"
