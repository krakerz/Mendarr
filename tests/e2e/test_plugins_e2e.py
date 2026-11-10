"""Plugin system end to end: per-domain plugins, auto-rollback, the patchright backend, worker isolation."""

import json
from pathlib import Path

import httpx
import pytest
from e2e_env import FIXTURE, FIXTURE_HOST, cli, compose_exec, v1

pytestmark = pytest.mark.e2e

SCOPE = "fixture"  # registrable_domain() of http://fixture:8080
DEFAULT_SRC = (Path(__file__).resolve().parents[2] / "src/mendarr/plugins/builtin/_default.py").read_text()
BROKEN_SRC = '''from mendarr.sdk import Ctx, Page

META = {"backend": "camoufox", "notes": "e2e: always fails"}


async def solve(page: Page, ctx: Ctx) -> None:
    raise RuntimeError("broken on purpose")
'''
PATCHRIGHT_SRC = DEFAULT_SRC.replace('"backend": "camoufox"', '"backend": "patchright"')
EVIL_SRC = "import os\n" + BROKEN_SRC


def ok(proc) -> str:
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def plugins() -> list[dict]:
    return json.loads(ok(cli("plugins", "list", "--scope", SCOPE, "--json")))


def by_version(v: int) -> dict:
    return next(p for p in plugins() if p["version"] == v)


def import_plugin(source: str) -> int:
    ok(cli("plugins", "import", "-", "--scope", SCOPE, "--activate", stdin=source))
    return max(p["version"] for p in plugins())


def get(path: str) -> tuple[int, dict]:
    httpx.post(f"{FIXTURE_HOST}/reset", timeout=10)  # no valid cached clearance: the plugin runs every time
    return v1({"cmd": "request.get", "url": f"{FIXTURE}{path}"})


@pytest.fixture(autouse=True)
def no_fixture_plugin():
    cli("plugins", "retire", SCOPE)
    yield
    cli("plugins", "retire", SCOPE)


def test_guard_rejects_on_import():
    proc = cli("plugins", "import", "-", "--scope", SCOPE, stdin=EVIL_SRC)
    assert proc.returncode == 1
    assert "import os is not allowed" in proc.stderr


def test_domain_plugin_wins_over_default():
    v = import_plugin(BROKEN_SRC)
    code, body = get("/cf/interactive")
    assert code == 500 and body["status"] == "error"
    assert by_version(v)["fail"] == 1
    ok(cli("plugins", "retire", SCOPE))
    code, body = get("/cf/interactive")
    assert code == 200 and body["message"] == "Challenge solved!"


def test_auto_rollback_to_parent():
    good = import_plugin(DEFAULT_SRC)
    bad = import_plugin(BROKEN_SRC)
    assert by_version(bad)["parent"] == good
    for _ in range(3):  # MENDARR_ROLLBACK_MIN_SAMPLES in the test env
        assert get("/cf/interactive")[0] == 500
    assert by_version(bad)["status"] == "retired"
    assert by_version(good)["status"] == "active"
    incidents = json.loads(ok(cli("incidents", "list", "--json")))
    assert any(i["domain"] == SCOPE and i["state"] == "open" and i["plugin"] == f"{SCOPE}@v{bad}" for i in incidents)
    code, body = get("/cf/interactive")
    assert code == 200 and body["message"] == "Challenge solved!"
    assert by_version(good)["ok"] >= 1


def test_patchright_backend():
    import_plugin(PATCHRIGHT_SRC)
    code, body = get("/cf/pass")
    assert code == 200, body.get("message")
    assert body["message"] == "Challenge solved!"
    assert "Chrome/" in body["solution"]["userAgent"]


def test_worker_requires_token():
    probe = (
        "import urllib.request as u, urllib.error as e\n"
        "try:\n    u.urlopen('http://mendarr-browser:8192/info', timeout=5)\n    print(200)\n"
        "except e.HTTPError as x:\n    print(x.code)\n"
    )
    assert ok(compose_exec("mendarr", "python", "-c", probe)).strip() == "401"


def test_worker_is_unprivileged():
    out = ok(compose_exec(
        "mendarr-browser", "sh", "-c",
        "id -u; test -e /data && echo has-data; touch /app/.venv/probe 2>/dev/null && echo venv-writable; true",
    ))
    lines = out.split()
    assert lines[0] != "0"
    assert "has-data" not in lines and "venv-writable" not in lines
