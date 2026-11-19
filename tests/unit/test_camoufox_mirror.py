import hashlib
import io
import json
import os
import subprocess
import sys
import types
import zipfile

import httpx
import pytest

from mendarr.config import Settings
from mendarr.worker import camoufox_mirror as cm
from mendarr.worker.backend_updates import BackendUpdater

API = "https://api.github.com/repos/jo-inc/camofox-browser/releases"


def zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("camoufox-bin", "#!/bin/sh\n")
        zf.writestr("sub/data.txt", "x")
    return buf.getvalue()


def asset(version, arch="x86_64", digest="sha256:" + "a" * 64):
    name = f"camoufox-{version}-lin.{arch}.zip"
    a = {"name": name, "browser_download_url": f"https://github.com/dl/{name}", "created_at": "2026-11-01T00:00:00Z"}
    if digest:
        a["digest"] = digest
    return a


def rel(version, tag="camoufox-backup-1", **kw):
    other = asset(version, "arm64", digest="sha256:" + "b" * 64)
    return {"tag_name": tag, "name": f"Camoufox backup v{version}", "assets": [other, asset(version, **kw)]}


def releases_transport(data):
    return httpx.MockTransport(lambda req: httpx.Response(200, json=data))


@pytest.fixture(autouse=True)
def x86(monkeypatch):
    monkeypatch.setattr(cm.platform, "machine", lambda: "x86_64")


def test_latest_picks_newest_and_arch():
    data = [
        rel("156.0.1-beta.36", "camoufox-backup-2"),
        rel("158.0.1-beta.41", "camoufox-backup-3", digest="sha256:" + "c" * 64),
        rel("152.0.4-beta.30", "camoufox-backup-1"),
        rel("999.0.0-beta.1", "v1.19.0"),  # not a backup release
        rel("158.0.1-beta.9", "camoufox-backup-4"),
    ]
    b = cm.latest(API, releases_transport(data))
    assert b.version == "158.0.1-beta.41" and b.sha256 == "c" * 64
    assert b.asset_name == "camoufox-158.0.1-beta.41-lin.x86_64.zip"


def test_latest_arm64(monkeypatch):
    monkeypatch.setattr(cm.platform, "machine", lambda: "aarch64")
    b = cm.latest(API, releases_transport([rel("158.0.1-beta.41")]))
    assert b.asset_name.endswith("lin.arm64.zip") and b.sha256 == "b" * 64


def test_latest_refuses_missing_digest():
    with pytest.raises(cm.MirrorError, match="sha256"):
        cm.latest(API, releases_transport([rel("158.0.1-beta.41", digest=None)]))


def test_latest_without_backup_release():
    with pytest.raises(cm.MirrorError):
        cm.latest(API, releases_transport([rel("1.0.0-beta.1", "v1.19.0")]))


def build_for(data):
    return cm.MirrorBuild(
        version="158.0.1-beta.41",
        asset_url="https://github.com/dl/x.zip",
        sha256=hashlib.sha256(data).hexdigest(),
        asset_name="x.zip",
    )


def test_install_rejects_mismatch_and_cleans_up(tmp_path):
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"tampered"))
    with pytest.raises(cm.MirrorError, match="mismatch"):
        cm.install(build_for(zip_bytes()), tmp_path, transport)
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == []


def test_install_layout(tmp_path):
    data = zip_bytes()
    dest = cm.install(build_for(data), tmp_path, httpx.MockTransport(lambda req: httpx.Response(200, content=data)))
    sha8 = hashlib.sha256(data).hexdigest()[:8]
    assert dest == tmp_path / "camoufox" / "browsers" / "official" / f"158.0.1-beta.41-{sha8}"
    assert (dest / "camoufox-bin").stat().st_mode & 0o111
    meta = json.loads((dest / "version.json").read_text())
    assert (meta["version"], meta["build"], meta["prerelease"]) == ("158.0.1", "beta.41", True)
    cfg = json.loads((tmp_path / "camoufox" / "config.json").read_text())
    assert cfg["active_version"] == f"browsers/official/158.0.1-beta.41-{sha8}" and cfg["pinned"] == "158.0.1-beta.41"
    assert [p.name for p in tmp_path.iterdir()] == ["camoufox"]  # no temp zip left behind


def test_install_camoufox_sees_version(tmp_path):
    pytest.importorskip("camoufox.pkgman")
    data = zip_bytes()
    cm.install(build_for(data), tmp_path, httpx.MockTransport(lambda req: httpx.Response(200, content=data)))
    out = subprocess.run(
        [sys.executable, "-c", "from camoufox.pkgman import installed_verstr; print(installed_verstr())"],
        env={**os.environ, "XDG_CACHE_HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip().endswith("158.0.1-beta.41"), out.stderr


def updater(tmp_path, monkeypatch, official_ok=False, run=None, **kw):
    def fake_update():
        if not official_ok:
            raise RuntimeError("404 Client Error")
        return types.SimpleNamespace(verstr="150.0-beta.1")

    mod = types.ModuleType("camoufox.__main__")
    mod.CamoufoxUpdate = fake_update
    monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.__main__", mod)

    class Pool:
        def retire(self, backend):
            pass

    return BackendUpdater(
        Settings(backends_dir=str(tmp_path), **kw),
        Pool(),
        is_idle=lambda: True,
        request_restart=lambda: None,
        transport=releases_transport([rel("158.0.1-beta.41")]),
        run=run or (lambda *a, **k: None),
    )


async def test_check_falls_back_to_mirror(tmp_path, monkeypatch):
    up = updater(tmp_path, monkeypatch)
    assert await up._latest_camoufox() == "158.0.1-beta.41"
    assert up._status("camoufox").latest_via == "mirror"


async def test_check_official_has_no_via(tmp_path, monkeypatch):
    up = updater(tmp_path, monkeypatch, official_ok=True)
    assert await up._latest_camoufox() == "150.0-beta.1"
    assert up._status("camoufox").latest_via == ""


async def test_check_without_mirror_keeps_message(tmp_path, monkeypatch):
    up = updater(tmp_path, monkeypatch, camoufox_mirror_url="")
    with pytest.raises(RuntimeError, match="maintainer accounts suspended"):
        await up._latest_camoufox()


async def test_update_falls_back_to_mirror_install(tmp_path, monkeypatch):
    data = zip_bytes()
    seen = []

    async def run(cmd, *, env, timeout):
        seen.append(cmd[0])
        return (1, "404") if cmd[0] == "camoufox" else (0, "Mozilla Firefox/158")

    def fake_install(build, root, transport=None):
        (root / "camoufox" / "browsers" / "official" / "158.0.1-beta.41").mkdir(parents=True)
        (root / "camoufox" / "config.json").write_text("{}")

    up = updater(tmp_path, monkeypatch, run=run)
    monkeypatch.setattr(cm, "latest", lambda url, transport=None: build_for(data))
    monkeypatch.setattr(cm, "install", fake_install)
    st = await up.update("camoufox")
    assert st.state == "pending_restart" and st.latest_via == "mirror" and seen == ["camoufox", "mendarr-worker"]


def test_latest_exact_version():
    data = [rel("156.0.1-beta.36", "camoufox-backup-2"), rel("158.0.1-beta.41", "camoufox-backup-3")]
    assert cm.latest(API, releases_transport(data), version="156.0.1-beta.36").version == "156.0.1-beta.36"
    with pytest.raises(cm.MirrorError, match="157"):
        cm.latest(API, releases_transport(data), version="157.0")


def test_install_without_force_pin_leaves_choice_unset(tmp_path):
    data = zip_bytes()
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=data))
    cm.install(build_for(data), tmp_path, transport, force_pin=False)
    assert set(json.loads((tmp_path / "camoufox" / "config.json").read_text())) == {"active_version"}


def test_pinned_version_reads_pin_file(tmp_path, monkeypatch):
    pkg = tmp_path / "pkgs" / "camoufox"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("raise RuntimeError('must not be imported')")
    pin = {"tag": "v156.0.1-beta.36", "version": "156.0.1", "build": "beta.36"}
    (pkg / "browser-pin.json").write_text(json.dumps(pin))
    monkeypatch.syspath_prepend(str(tmp_path / "pkgs"))
    monkeypatch.delitem(sys.modules, "camoufox", raising=False)
    assert cm.pinned_version() == "156.0.1-beta.36"


async def test_camoufox_fetch_exit_0_without_a_build_falls_back_to_mirror(tmp_path, monkeypatch):
    # camoufox fetch prints the 404 and still exits 0: the empty staging dir must trigger the mirror
    seen = []

    async def run(cmd, *, env, timeout):
        seen.append(cmd[0])
        return (0, "not in official's releases") if cmd[0] == "camoufox" else (0, "Mozilla Firefox/158")

    def fake_install(build, root, transport=None):
        (root / "camoufox" / "browsers" / "official" / "158.0.1-beta.41").mkdir(parents=True)
        (root / "camoufox" / "config.json").write_text("{}")

    up = updater(tmp_path, monkeypatch, run=run)
    monkeypatch.setattr(cm, "latest", lambda url, transport=None: object())
    monkeypatch.setattr(cm, "install", fake_install)
    st = await up.update("camoufox")
    assert st.state == "pending_restart" and st.latest_via == "mirror" and seen == ["camoufox", "mendarr-worker"]


def test_main_with_explicit_url_does_not_import_config(monkeypatch):
    monkeypatch.delitem(sys.modules, "mendarr.config", raising=False)
    # empty URL = mirror off; must be judged without consulting Settings
    assert cm.main(["install", "--url", ""]) == 1
    assert "mendarr.config" not in sys.modules


def test_script_runs_standalone_with_empty_url(tmp_path):
    script = tmp_path / "cfmirror.py"
    script.write_text(open(cm.__file__).read())
    out = subprocess.run(
        [sys.executable, "-I", str(script), "install", "--pinned", "--url", ""],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert out.returncode == 1
    assert "no Camoufox mirror URL" in out.stderr
