"""Manual solve end to end; the click travels over RFB so it crosses dashboard auth, the API WS proxy, the worker bridge, x11vnc and X11 (xdotool in the worker would skip all of that)."""

import json
import os
import re
import threading
import time

import httpx
import pytest
from e2e_env import API, FIXTURE_HOST, compose_exec, v1
from rfb import RfbClient

pytestmark = pytest.mark.e2e

USER = os.environ.get("MENDARR_E2E_UI_USER", "admin")
PASSWORD = os.environ.get("MENDARR_E2E_UI_PASSWORD", "admin")
URL = "http://fixture:8080/cf/interactive?widget=fake&big=1"

SETUP = f"""
import json, time
from pathlib import Path
from mendarr.core.cookies import CookieCache
from mendarr.core.domains import registrable_domain
from mendarr.plugins.store import PluginStore
url = {URL!r}
d = registrable_domain(url)
CookieCache.open(Path("/data/store.db"), max_ttl_s=60).purge(d)
s = PluginStore.open(Path("/data"))
s.resolve_domain(d, "e2e reset")
iid = s.note_failure(d, kind="challenge_loop", url=url, trace_id="", plugin="_default@builtin")
s.update_incident(iid, state="manual", tier=3, next_attempt_at=time.time() + 3600)
print(json.dumps({{"id": iid, "domain": d}}))
"""

STATE = """
from pathlib import Path
from mendarr.plugins.store import PluginStore
print(PluginStore.open(Path('/data')).incident({iid})['state'])
"""

DEMO = """
import json
from pathlib import Path
from mendarr.core.traces import TraceStore
t = TraceStore(Path('/data/traces')).latest({domain!r}, 'demo')
print(json.dumps(t.meta if t else None))
"""

RESOLVE = """
from pathlib import Path
from mendarr.plugins.store import PluginStore
PluginStore.open(Path('/data')).resolve_domain({domain!r}, 'e2e cleanup')
"""


def py(code: str) -> str:
    r = compose_exec("mendarr", "python", "-c", code)
    assert r.returncode == 0, r.stderr[-800:]
    return r.stdout.strip()


def test_manual_solve_through_vnc():
    httpx.post(f"{FIXTURE_HOST}/reset", timeout=10)
    info = json.loads(py(SETUP))
    iid, domain = info["id"], info["domain"]
    held: dict = {}

    def hold() -> None:
        held["status"], held["body"] = v1({"cmd": "request.get", "url": URL, "maxTimeout": 120000})
        held["t_done"] = time.time()

    rfb = None
    sid = None
    token = ""
    client = httpx.Client(base_url=API, follow_redirects=False, timeout=30)
    try:
        thread = threading.Thread(target=hold)
        thread.start()
        time.sleep(3)
        assert thread.is_alive(), "request was not held"

        r = client.post("/ui/login", data={"user": USER, "password": PASSWORD, "next": "/ui"})
        assert r.status_code == 303
        token = re.search(r'name="csrf-token" content="([0-9a-f]+)"', client.get("/ui").text).group(1)
        r = client.post(f"/ui/incidents/{iid}/manual", headers={"X-CSRF-Token": token})
        assert r.status_code == 303, r.text[:300]
        sid = r.headers["location"].rstrip("/").rsplit("/", 1)[-1]
        page = client.get(f"/ui/manual/{sid}")
        assert page.status_code == 200
        slot = re.search(r'data-slot="(\d+)"', page.text).group(1)

        cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
        rfb = RfbClient(f"{API.replace('http', 'ws', 1)}/ui/vnc/{slot}", {"Cookie": cookie, "Origin": API})
        w, h = rfb.handshake()
        time.sleep(3)  # page load
        t_click = time.time()
        rfb.click(w // 2, h // 2)
        retries = [(w // 2, h * 2 // 3), (w // 3, h // 2)]
        solved = False
        for i in range(30):
            time.sleep(1)
            if "Solved" in client.get(f"/ui/manual/{sid}/status").text:
                solved = True
                break
            if i == 9 and retries:  # window geometry isn't pinned: probe other spots
                rfb.click(*retries.pop(0))
            elif i == 14 and retries:
                rfb.click(*retries.pop(0))
        assert solved, "manual session never reported Solved"

        thread.join(timeout=30)
        assert not thread.is_alive()
        assert held["status"] == 200 and held["body"]["status"] == "ok", held
        assert held["t_done"] > t_click
        names = [c["name"] for c in held["body"]["solution"]["cookies"]]
        assert "cf_clearance" in names

        t0 = time.time()
        status, body = v1({"cmd": "request.get", "url": URL, "maxTimeout": 60000}, timeout=30)
        assert status == 200 and body["status"] == "ok"
        assert body["message"] == "Challenge not detected!"
        assert time.time() - t0 < 15

        assert py(STATE.format(iid=iid)) == "solved"
        meta = json.loads(py(DEMO.format(domain=domain)))
        assert meta and meta["kind"] == "demo"
        demo = meta["extra"]["demo"]
        clicks = [e for e in demo["events"] if e.get("type") == "click"]
        assert any(
            e.get("trusted")
            and "/cdn-cgi/challenge-platform/fake/turnstile" in e.get("frame_url", "")
            and "#cb" in e.get("selector", "")
            for e in clicks
        ), demo["events"]
        assert "cf_clearance" in [c["name"] for c in demo["cookies"]]
        assert all("value" not in c for c in demo["cookies"])
        assert meta["steps"]
    finally:
        if rfb is not None:
            rfb.close()
        if sid:
            try:
                client.post(f"/ui/manual/{sid}/cancel", headers={"X-CSRF-Token": token})
            except Exception:
                pass
        client.close()
        py(RESOLVE.format(domain=domain))
