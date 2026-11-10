"""Healer end to end: fault-injected plugin, fake LLM (deterministic) and an opt-in real LLM."""

import json
import os
import time
from pathlib import Path

import httpx
import pytest
from e2e_env import COMPOSE, cli, v1

pytestmark = pytest.mark.e2e

FAKELLM = os.environ.get("MENDARR_E2E_FAKELLM", "http://127.0.0.1:8182").rstrip("/")
REAL_LLM = os.environ.get("MENDARR_E2E_LLM") == "1"

# Fault injection: wrong target (a wrong selector alone would still hit the checkbox, because
# `click_point` falls back to the frame's inset point).
BROKEN = '''META = {"backend": "camoufox", "notes": "fault injection: clicks the page heading, not the widget"}


async def solve(page, ctx):
    if not await ctx.wait_challenge(timeout=10):
        return
    await ctx.human_click(page.locator("#challenge-stage h1"))
    await ctx.wait_clearance(timeout=6)
'''


def ok(proc) -> str:
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def seed_broken(host: str) -> None:
    ok(cli("plugins", "import", "-", "--scope", host, "--activate", stdin=BROKEN))


def incident(host: str) -> dict | None:
    rows = [i for i in json.loads(ok(cli("incidents", "list", "--json"))) if i["domain"] == host]
    return max(rows, key=lambda i: i["id"]) if rows else None


def close_open(host: str) -> None:
    # the test env's store persists between runs; a leftover manual incident would absorb the new failure
    for i in json.loads(ok(cli("incidents", "list", "--json"))):
        if i["domain"] == host and i["state"] not in ("solved", "gave_up"):
            ok(cli("incidents", "close", str(i["id"]), "--gave-up"))


def wait_state(host: str, states: set[str], timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    inc = None
    while time.monotonic() < deadline:
        inc = incident(host)
        if inc and inc["state"] in states:
            return inc
        time.sleep(3)
    detail = cli("incidents", "show", str(inc["id"])).stdout if inc else "no incident"
    pytest.fail(f"{host}: wanted {sorted(states)}, last state {inc and inc['state']}\n{detail}")


def trigger(host: str) -> None:
    _, body = v1({"cmd": "request.get", "url": f"http://{host}:8080/cf/interactive", "maxTimeout": 40000})
    assert body["status"] == "error", body


def attempts(inc: dict) -> list[dict]:
    shown = json.loads(ok(cli("incidents", "show", str(inc["id"]))))
    return shown["attempts"]


def fake_state() -> dict:
    return httpx.get(f"{FAKELLM}/state", timeout=10).json()


def active_plugin(host: str) -> dict:
    rows = json.loads(ok(cli("plugins", "list", "--scope", host, "--json")))
    return next(p for p in rows if p["status"] == "active")


def test_heals_with_fake_llm():
    httpx.post(f"{FAKELLM}/reset", json={"script": ["guard", "fixed"]}, timeout=10).raise_for_status()
    close_open("fixture-heal")
    seed_broken("fixture-heal")
    trigger("fixture-heal")
    inc = wait_state("fixture-heal", {"solved"}, 240)

    assert [a["result"] for a in attempts(inc)] == ["guard", "passed"]
    plugin = active_plugin("fixture-heal")
    assert plugin["author"] == "llm"
    assert (plugin.get("notes") or "").startswith("fake LLM")

    state = fake_state()
    assert len(state["completions"]) == 2
    second = json.dumps(state["completions"][1]["messages"])
    assert "Previous attempts" in second and "guard" in second
    hooks = state["hooks"]
    assert any(
        h["path"] == "/hook/generic"
        and h.get("json", {}).get("event") == "healed"
        and h["json"].get("domain") == "fixture-heal"
        for h in hooks
    )
    assert any(h["path"] == "/hook/discord" for h in hooks)

    trace = Path(COMPOSE).parent / "data" / "traces" / inc["trace_id"]
    assert (trace / "meta.json").is_file() and (trace / "screenshot.png").is_file()
    # the widget iframe sits in a closed shadow root (like real Turnstile), so it shows up as an appended frame section
    assert "<!-- frame " in (trace / "dom.html").read_text()
    assert "fake/turnstile" in (trace / "dom.html").read_text()

    status, body = v1({"cmd": "request.get", "url": "http://fixture-heal:8080/cf/interactive", "maxTimeout": 40000})
    assert status == 200 and body["status"] == "ok"
    assert body["message"] == "Challenge solved!"


def test_escalates_to_manual():
    httpx.post(f"{FAKELLM}/reset", json={"script": ["guard"]}, timeout=10).raise_for_status()
    close_open("fixture-heal")
    seed_broken("fixture-heal")  # re-activates a broken version; the previous incident is solved, so a new one opens
    trigger("fixture-heal")
    inc = wait_state("fixture-heal", {"manual"}, 180)

    assert [a["result"] for a in attempts(inc)] == ["guard"] * 9
    hooks = fake_state()["hooks"]
    assert any(h["path"] == "/hook/generic" and h.get("json", {}).get("event") == "manual" for h in hooks)
    assert any(h["path"] == "/hook/discord" and h.get("has_png") for h in hooks)


@pytest.mark.skipif(not REAL_LLM, reason="MENDARR_E2E_LLM=1 not set")
def test_heals_with_real_llm():
    print(cli("healer", "check").stdout)
    seed_broken("fixture-llm")
    trigger("fixture-llm")
    timeout = float(os.environ.get("MENDARR_E2E_LLM_TIMEOUT", "2700"))
    inc = wait_state("fixture-llm", {"solved", "manual", "gave_up"}, timeout)
    for a in attempts(inc):
        print(a.get("result"), a.get("model"), str(a.get("detail"))[:200])
    assert inc["state"] == "solved"
    assert active_plugin("fixture-llm")["author"] == "llm"
