import base64

from fakeworker import snapshot

from mendarr.core.traces import TraceStore, trim_html
from mendarr.rpc import Cookie, SolveResult, TraceData, WorkerError

PNG = b"\x89PNG\r\n\x1a\nfake"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def result(trace=True) -> SolveResult:
    data = TraceData(
        screenshot_b64=base64.b64encode(PNG).decode(), dom="<div>x</div>", console=["error: y"], steps=["s"]
    )
    return SolveResult(
        challenge_seen=True,
        clicks=2,
        error=WorkerError(type="plugin", detail="boom"),
        snapshot=snapshot(
            "http://example.com/x", status=403, title="Just a moment...",
            html="<title>x</title><script>evil()</script><p>hi</p>",
            headers={"cf-mitigated": "challenge", "set-cookie": "a=b", "server": "cloudflare"},
            cookies=[Cookie(name="sid", value="secret", domain="example.com")],
        ),
        trace=data if trace else None,
    )


def test_save_with_trace(tmp_path):
    ts = TraceStore.open(tmp_path / "traces")
    tid = ts.save(domain="example.com", url="http://example.com/x", kind="failure", result=result(), failure="plugin")
    d = tmp_path / "traces" / tid
    assert (d / "screenshot.png").read_bytes() == PNG
    assert (d / "dom.html").read_text() == "<div>x</div>"
    t = ts.load(tid)
    assert t.screenshot == PNG and t.dom == "<div>x</div>"
    m = t.meta
    assert m["headers"] == {"cf-mitigated": "challenge", "server": "cloudflare"}
    assert m["cookies"] == ["sid"] and m["status"] == 403 and m["has_screenshot"]
    assert m["error"] == {"type": "plugin", "detail": "boom"} and m["console"] == ["error: y"] and m["steps"] == ["s"]


def test_save_without_trace_uses_snapshot(tmp_path):
    ts = TraceStore.open(tmp_path)
    tid = ts.save(domain="example.com", url="u", kind="failure", result=result(trace=False))
    t = ts.load(tid)
    assert "evil" not in t.dom and "<p>hi</p>" in t.dom
    assert t.screenshot is None and not t.meta["has_screenshot"]


def test_trim_html():
    out = trim_html("<noscript>a</noscript><!-- c --><style>x{}</style><svg><path/></svg>  a \n b", limit=20)
    assert out.startswith("<svg/> a b")
    assert trim_html("x" * 100, limit=10) == "x" * 10 + "\n<!-- truncated -->"


def test_load_rejects_bad_ids(tmp_path):
    ts = TraceStore.open(tmp_path)
    assert ts.load("../x") is None
    assert ts.load("nope") is None


def test_latest_and_prune(tmp_path):
    clock = Clock()
    ts = TraceStore(tmp_path, clock)
    ids = []
    for kind in ["failure", "failure", "demo", "failure", "demo", "failure"]:
        clock.t += 1
        ids.append(ts.save(domain="example.com", url="u", kind=kind, result=None))
    clock.t += 1
    other = ts.save(domain="other.com", url="u", kind="failure", result=None)
    assert ts.latest("example.com").id == ids[-1]
    assert ts.latest("example.com", "demo").id == ids[4]
    assert ts.latest("example.com", "replay") is None
    assert ts.prune("example.com", keep=2, keep_demos=1) == 3
    assert {p.name for p in tmp_path.iterdir()} == {ids[5], ids[3], ids[4], other}


def test_should_save_rate_limit():
    clock = Clock()
    ts = TraceStore(None, clock)
    assert ts.should_save("a.com", 30)
    clock.t += 10
    assert not ts.should_save("a.com", 30)
    assert ts.should_save("b.com", 30)
    clock.t += 25
    assert ts.should_save("a.com", 30)


def test_no_root_saves_nothing():
    ts = TraceStore(None)
    assert ts.save(domain="a.com", url="u", kind="failure", result=None) is None
    assert ts.latest("a.com") is None and ts.prune("a.com", 1) == 0
