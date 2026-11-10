import pytest

from mendarr.core.traces import Trace
from mendarr.healer import prompt
from mendarr.healer.config import LlmProfile
from mendarr.healer.prompt import PromptError, build_messages, extract_plugin, switch_backend

SRC = 'META = {"backend": "camoufox", "notes": "n"}\n\n\nasync def solve(page, ctx):\n    return None\n'


def test_extract_one_block():
    assert extract_plugin(f"text\n```python\n{SRC}```\nbye") == SRC


def test_extract_untagged_and_last():
    assert extract_plugin(f"```\n{SRC}```") == SRC
    other = SRC.replace('"n"', '"second"')
    assert extract_plugin(f"```python\n{SRC}```\nrevised\n```py\n{other}```") == other


def test_extract_ignores_think():
    text = f"<think>\n```python\n{SRC}```\n</think>\nno code here"
    with pytest.raises(PromptError):
        extract_plugin(text)


def test_extract_needs_solve():
    with pytest.raises(PromptError):
        extract_plugin("```python\nprint(1)\n```")
    with pytest.raises(PromptError):
        extract_plugin('```json\n{"def solve": 1}\n```')


def test_switch_backend_both_ways():
    flipped = switch_backend(SRC)
    assert "'backend': 'patchright'" in flipped.splitlines()[0]
    assert flipped.splitlines()[1:] == SRC.splitlines()[1:]
    obscura = switch_backend(flipped)
    assert "'backend': 'obscura'" in obscura.splitlines()[0]
    back = switch_backend(obscura)
    assert "'backend': 'camoufox'" in back and "async def solve" in back


def test_switch_backend_default_and_none():
    src = 'META = {"notes": "x"}\n\nasync def solve(page, ctx):\n    pass\n'
    assert "patchright" in switch_backend(src)
    assert switch_backend("async def solve(page, ctx):\n    pass\n") is None
    assert switch_backend("def (") is None


def trace(**kw) -> Trace:
    meta = {
        "final_url": "http://e.com/f", "status": 403, "title": "Just a moment...", "challenge_seen": True,
        "clicks": 1, "error": None, "headers": {"cf-mitigated": "challenge", "server": "x"},
        "steps": ["clicked"], "console": ["error: boom"],
        "network": [{"url": f"http://e.com/{i}.js", "status": 200, "type": "script"} for i in range(60)]
        + [
            {"url": "http://e.com/bad", "status": 404, "type": "fetch"},
            {"url": "http://e.com/cf", "status": 200, "type": "doc", "cf": "ray1"},
        ],
        "frames": [{"url": "https://challenges.cloudflare.com/x", "name": "", "parent": "http://e.com/"}],
    }
    meta.update(kw.pop("meta", {}))
    return Trace("e.com--1-abcdef", meta, kw.pop("dom", "<html>" + "x" * 500 + "</html>"), kw.pop("shot", None))


def build(**kw):
    args = dict(
        domain="e.com", url="http://e.com/x", kind="challenge_loop", detail="stuck", plugin_ref="e.com@v1",
        plugin_source=SRC, trace=trace(), history=[], demo=None, profile=LlmProfile(base_url="http://x/v1"),
    )
    args.update(kw)
    return build_messages(**args)


def text_of(msgs):
    c = msgs[1]["content"]
    return c if isinstance(c, str) else c[0]["text"]


def test_sections_present():
    msgs = build()
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == prompt.SYSTEM
    t = text_of(msgs)
    for h in (
        "## SDK reference", "## Failing plugin (e.com@v1)", "## Failure", "## What the plugin logged",
        "## Console", "## Network", "## Frames", "## Page DOM (trimmed)", "## Task",
    ):
        assert h in t
    assert "## Previous attempts" not in t and "## Demonstration" not in t
    assert "cf-mitigated" in t


def test_sections_absent_without_trace():
    t = text_of(build(trace=None))
    assert "## Failure" in t and "## Console" not in t and "## Page DOM" not in t


def test_dom_capped():
    t = text_of(build(profile=LlmProfile(base_url="http://x/v1", dom_chars=50)))
    assert "x" * 60 not in t


def test_network_prioritised():
    t = text_of(build())
    net = t.split("## Network\n")[1].split("\n\n")[0].splitlines()
    assert len(net) == 40
    assert net[0].startswith("404") and "cf=ray1" in net[1]


def test_vision():
    png = b"\x89PNGdata"
    assert isinstance(build(trace=trace(shot=png))[1]["content"], str)
    msgs = build(trace=trace(shot=png), profile=LlmProfile(base_url="http://x/v1", vision=True))
    assert msgs[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert isinstance(build(profile=LlmProfile(base_url="http://x/v1", vision=True))[1]["content"], str)


def test_history_and_demo():
    hist = [
        {"heal_window": 0, "round": 0, "result": "guard", "detail": "import os", "version": None, "scope": "",
         "trace_id": ""},
        {"heal_window": 1, "round": 1, "result": "replay_failed", "detail": "Q" * 600, "version": 3,
         "scope": "e.com", "trace_id": "e.com--2-abcdef", "source": SRC},
    ]
    demo = trace(meta={"extra": {"clicks": [[1, 2]]}, "steps": ["human clicked"]})
    t = text_of(build(history=hist, demo=demo, load_trace=lambda tid: trace(meta={"steps": ["replay step"]})))
    assert "window 1 round 1: guard" in t and "window 2 round 2: replay_failed" in t
    assert "### Last candidate (e.com@v3)" in t and "### Its replay" in t and "replay step" in t
    assert "Q" * 401 not in t and "Q" * 400 in t
    assert "## Demonstration by a human" in t and "human clicked" in t
