import random
import time

from mendarr.sdk import Ctx, human, turnstile


class FakeLocator:
    def __init__(self, n=0, value=""):
        self.n, self.value = n, value
        self.first = self

    async def count(self):
        return self.n

    async def input_value(self, timeout=None):
        return self.value


class FakePage:
    def __init__(self, titles, *, css_count=0, token=None):
        self.titles = list(titles)  # one per title() call, last repeats
        self.css_count = css_count
        self.token = token
        self.frames = []
        self.main_frame = object()

    async def title(self):
        return self.titles.pop(0) if len(self.titles) > 1 else self.titles[0]

    def locator(self, sel):
        if "cf-turnstile-response" in sel:
            return FakeLocator(1 if self.token else 0, self.token or "")
        return FakeLocator(self.css_count)


def ctx(page, secs=5, **kw):
    return Ctx(page, deadline=time.monotonic() + secs, **kw)


def test_inset_and_center_points():
    box = {"x": 100, "y": 200, "width": 300, "height": 65}
    for _ in range(50):
        x, y = turnstile.inset_point(box)
        assert 100 + 28 - 3 <= x <= 100 + 28 + 3 and 232.5 - 3 <= y <= 232.5 + 3
        x, y = turnstile.center_point(box)
        assert abs(x - 250) <= 3 and abs(y - 232.5) <= 3


def test_inset_narrow_box():
    x, _ = turnstile.inset_point({"x": 0, "y": 0, "width": 20, "height": 20}, random.Random(1))
    assert 7 <= x <= 13


def test_frame_matches():
    assert turnstile.frame_matches("https://challenges.cloudflare.com/cdn-cgi/x")
    assert turnstile.frame_matches("http://fixture/cdn-cgi/challenge-platform/h/b")
    assert not turnstile.frame_matches("https://example.com/")


async def test_challenge_present_by_title_and_css():
    assert await ctx(FakePage(["Just a moment..."])).challenge_present()
    assert await ctx(FakePage(["Home"], css_count=1)).challenge_present()
    assert not await ctx(FakePage(["Home"])).challenge_present()


async def test_challenge_present_by_header():
    async def headers():
        return {"cf-mitigated": "challenge"}

    assert await ctx(FakePage(["Home"]), main_headers=headers).challenge_present()


async def test_playwright_error_counts_as_present():
    page = FakePage(["Home"])

    async def boom():
        raise RuntimeError("context destroyed")

    page.title = boom
    assert await ctx(page).challenge_present()


async def test_wait_challenge_false_after_two_clear_polls():
    assert await ctx(FakePage(["Home"])).wait_challenge(timeout=3) is False


async def test_wait_challenge_true_when_present():
    assert await ctx(FakePage(["Just a moment..."])).wait_challenge(timeout=3) is True


async def test_wait_clearance():
    page = FakePage(["Just a moment...", "Just a moment...", "Home"])
    assert await ctx(page).wait_clearance(timeout=3) is True
    assert await ctx(FakePage(["Just a moment..."])).wait_clearance(timeout=0.5) is False


async def test_find_turnstile_frame():
    page = FakePage(["x"])
    f = type("F", (), {"url": "https://challenges.cloudflare.com/x"})()
    page.frames = [page.main_frame, f]
    assert await ctx(page).find_turnstile_frame(timeout=1) is f
    page.frames = [page.main_frame]
    assert await ctx(page).find_turnstile_frame(timeout=0.3) is None


async def test_turnstile_token():
    assert await ctx(FakePage(["x"], token="abc")).turnstile_token() == "abc"
    assert await ctx(FakePage(["x"])).turnstile_token() is None


async def test_human_click_tuple_uses_backend(monkeypatch):
    seen = {}

    async def fake_click(page, x, y, *, display, backend):
        seen.update(x=x, y=y, display=display, backend=backend)
        return True, "mouse"

    monkeypatch.setattr(human, "click", fake_click)
    c = ctx(FakePage(["x"]), display=2, click_backend="mouse")
    assert await c.human_click((10.0, 20.0)) is True
    assert c.clicks == 1 and seen == {"x": 10.0, "y": 20.0, "display": 2, "backend": "mouse"}
    assert c.steps == ["click at 10,20 via mouse"]


async def test_mouse_backend_never_uses_xdotool():
    calls = []

    class Mouse:
        async def move(self, x, y, steps=1):
            calls.append("move")

        async def down(self):
            calls.append("down")

        async def up(self):
            calls.append("up")

    page = type("P", (), {"mouse": Mouse()})()
    assert await human.click(page, 5, 5, display=1, backend="mouse") == (True, "mouse")
    assert calls == ["move", "down", "up"]


def test_remaining_capped_at_zero():
    c = Ctx(FakePage(["x"]), deadline=time.monotonic() - 1)
    assert c.remaining() == 0.0
