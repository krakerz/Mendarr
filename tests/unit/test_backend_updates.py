import hashlib
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from mendarr.config import Settings
from mendarr.worker import backend_updates
from mendarr.worker.backend_updates import BackendUpdater, camoufox_overlay

DL = "https://github.com/h4ckf0r0day/obscura/releases/download/v1.2.0/obscura-x86_64-linux.tar.gz"


def tarball(name="obscura", body=b"#!/bin/sh\n"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name)
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def release(tag="v1.2.0", url=DL, digest=None):
    asset = {"name": "obscura-x86_64-linux.tar.gz", "browser_download_url": url}
    if digest:
        asset["digest"] = digest
    return {"tag_name": tag, "assets": [asset]}


class Pool:
    def __init__(self):
        self.retired = []

    def retire(self, backend):
        self.retired.append(backend)


def make(tmp_path, payload=None, rel=None, run=None, **kw):
    data = tarball() if payload is None else payload
    rel = rel or release(digest="sha256:" + hashlib.sha256(data).hexdigest())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=rel)
        return httpx.Response(200, content=data)

    restarts = []
    pool = Pool()
    up = BackendUpdater(
        Settings(backends_dir=str(tmp_path), **kw),
        pool,
        is_idle=lambda: True,
        request_restart=lambda: restarts.append(1),
        transport=httpx.MockTransport(handler),
        run=run or (lambda *a, **k: None),
    )

    async def smoke_ok(binary):
        assert binary.name == "obscura"

    up._smoke_obscura = smoke_ok
    return up, pool, restarts


async def _raise():
    raise RuntimeError("offline")


async def test_check_parses_tag_and_asset(tmp_path):
    up, _, _ = make(tmp_path)

    async def no_installed():
        pass

    up.refresh_installed = no_installed
    up._latest_camoufox = _raise
    state = await up.check()
    ob = next(b for b in state.backends if b.name == "obscura")
    assert ob.latest == "v1.2.0" and ob.state == "idle" and ob.checked_at > 0
    cf = next(b for b in state.backends if b.name == "camoufox")
    assert cf.state == "failed" and cf.error.startswith("check failed")


async def test_update_happy_path(tmp_path):
    up, pool, _ = make(tmp_path)
    st = await up.update("obscura")
    assert st.state == "idle" and st.installed == "v1.2.0" and st.source == "updated" and st.can_revert
    base = tmp_path / "obscura"
    assert (base / "current").resolve() == (base / "versions" / "v1.2.0").resolve()
    assert (base / "current" / "obscura").stat().st_mode & 0o111
    assert (base / "current" / "VERSION").read_text().strip() == "v1.2.0"
    assert pool.retired == ["obscura"]
    assert [p.name for p in base.iterdir() if p.name.startswith("staging")] == []
    manifest = json.loads((base / "current" / "MANIFEST.json").read_text())
    assert manifest["version"] == "v1.2.0" and list(manifest["files"]) == ["obscura"]


async def test_digest_mismatch_fails_and_keeps_current(tmp_path):
    up, pool, _ = make(tmp_path, rel=release(digest="sha256:" + "0" * 64))
    st = await up.update("obscura")
    assert st.state == "failed" and "sha256" in st.error
    assert not (tmp_path / "obscura" / "current").exists() and pool.retired == []
    assert [p.name for p in (tmp_path / "obscura").iterdir()] == []


async def test_off_host_download_refused(tmp_path):
    up, _, _ = make(tmp_path, rel=release(url="https://evil.example/obscura.tar.gz"))
    st = await up.update("obscura")
    assert st.state == "failed" and "outside" in st.error


async def test_smoke_failure_rolls_back(tmp_path):
    up, pool, _ = make(tmp_path)

    async def bad(binary):
        raise RuntimeError("the new obscura did not start")

    up._smoke_obscura = bad
    st = await up.update("obscura")
    assert st.state == "failed" and "did not start" in st.error
    assert not list((tmp_path / "obscura").iterdir())
    assert pool.retired == []


async def test_prune_keeps_two_and_revert(tmp_path):
    up, pool, _ = make(tmp_path)
    base = tmp_path / "obscura"
    for tag in ("v1.0.0", "v1.1.0", "v1.2.0"):
        up._asset = {"tag": tag, "url": DL, "digest": ""}
        assert (await up.update("obscura")).state == "idle"
    assert sorted(p.name for p in (base / "versions").iterdir()) == ["v1.1.0", "v1.2.0"]
    st = await up.revert("obscura")
    assert st.installed == "v1.1.0" and pool.retired[-1] == "obscura"


async def test_revert_without_previous_goes_built_in(tmp_path):
    up, _, _ = make(tmp_path)
    await up.update("obscura")
    st = await up.revert("obscura")
    assert not (tmp_path / "obscura" / "current").exists() and st.source == "built-in" and not st.can_revert


async def test_unknown_backend_is_a_key_error(tmp_path):
    up, _, _ = make(tmp_path)
    for name in ("patchright", "nope"):
        with pytest.raises(KeyError):
            await up.update(name)
        with pytest.raises(KeyError):
            await up.revert(name)


async def test_camoufox_update_pending_restart(tmp_path):
    calls = []

    async def run(cmd, *, env, timeout):
        calls.append((cmd[0], env["XDG_CACHE_HOME"]))
        if cmd[0] == "camoufox":
            (Path(env["XDG_CACHE_HOME"]) / "camoufox" / "browsers" / "official" / "150.0-beta.1").mkdir(parents=True)
            (Path(env["XDG_CACHE_HOME"]) / "camoufox" / "config.json").write_text("{}")
            return 0, "done"
        return 0, "Mozilla/5.0 Firefox/150.0"

    up, _, restarts = make(tmp_path, run=run)
    st = await up.update("camoufox")
    assert st.state == "pending_restart" and restarts == [1]
    assert [c[0] for c in calls] == ["camoufox", "mendarr-worker"] and calls[0][1] == calls[1][1]
    assert camoufox_overlay(Settings(backends_dir=str(tmp_path))) is not None
    manifest = json.loads((tmp_path / "camoufox" / "current" / "MANIFEST.json").read_text())
    assert sorted(manifest["files"]) == ["camoufox/config.json"]
    again = await up.update("camoufox")  # waits for the restart: no second download
    assert again.state == "pending_restart" and len(calls) == 2 and restarts == [1]


async def test_camoufox_smoke_failure(tmp_path):
    async def run(cmd, *, env, timeout):
        if cmd[0] == "camoufox":
            (Path(env["XDG_CACHE_HOME"]) / "camoufox" / "browsers" / "official" / "150.0-beta.1").mkdir(parents=True)
            return 0, "ok"
        return 1, "boom"

    up, _, restarts = make(tmp_path, run=run)
    st = await up.update("camoufox")
    assert st.state == "failed" and "test launch" in st.error and restarts == []
    assert list((tmp_path / "camoufox").iterdir()) == []


def test_overlay_none_for_empty_or_broken(tmp_path):
    s = Settings(backends_dir=str(tmp_path))
    assert camoufox_overlay(s) is None
    (tmp_path / "camoufox" / "versions" / "x").mkdir(parents=True)
    backend_updates._switch(tmp_path / "camoufox" / "current", "x")
    assert camoufox_overlay(s) is None  # no config.json
    assert camoufox_overlay(Settings(backends_dir="")) is None


def test_state_reports_auto_update_and_patchright(tmp_path):
    up, _, _ = make(tmp_path, backend_auto_update=True, backend_update_interval="10m")
    st = up.state()
    assert st.auto_update and st.interval_s == 3600.0
    pr = next(b for b in st.backends if b.name == "patchright")
    assert not pr.updatable and "rebuilding" in pr.note
    json.dumps(st.model_dump())


async def test_camoufox_404_is_readable(tmp_path, monkeypatch):
    import sys
    import types

    class CamoufoxUpdate:
        def __init__(self):
            raise RuntimeError("404 Client Error: Not Found for url: https://api.github.com/repos/x")

    fake = types.ModuleType("camoufox.__main__")
    fake.CamoufoxUpdate = CamoufoxUpdate
    monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.__main__", fake)
    up, _, _ = make(tmp_path)

    async def no_installed():
        pass

    up.refresh_installed = no_installed
    state = await up.check()
    cf = next(b for b in state.backends if b.name == "camoufox")
    assert cf.state == "failed" and "GitHub releases are unavailable" in cf.error

