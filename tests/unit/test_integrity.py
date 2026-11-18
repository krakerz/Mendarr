import logging

import pytest

from mendarr.config import Settings
from mendarr.worker import integrity
from mendarr.worker.backend_updates import BackendUpdater, _switch, camoufox_overlay
from mendarr.worker.backends import obscura


@pytest.fixture(autouse=True)
def clean_state():
    integrity.FAILED.clear()
    integrity._CHECKED.clear()
    yield
    integrity.FAILED.clear()
    integrity._CHECKED.clear()


def obscura_dir(tmp_path):
    return tmp_path / "obscura" / "versions" / "v1"


def fake_obscura(tmp_path, manifest=True):
    d = obscura_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "obscura").write_bytes(b"binary")
    (d / "obscura").chmod(0o755)
    if manifest:
        integrity.write_manifest(d, "v1", only=["obscura"])
    _switch(tmp_path / "obscura" / "current", "v1")
    return d


async def test_obscura_tampered_binary_falls_back(tmp_path, caplog):
    d = fake_obscura(tmp_path)
    s = Settings(backends_dir=str(tmp_path))
    assert await obscura._find_obscura_binary(s) == str(tmp_path / "obscura" / "current" / "obscura")
    (d / "obscura").write_bytes(b"binarX")
    with caplog.at_level(logging.WARNING):
        try:
            found = await obscura._find_obscura_binary(s)
        except obscura.BackendUnavailable:
            found = None  # no built-in obscura in this environment: still not the overlay
    assert found is None or "backends" not in found
    assert "failed its integrity check" in caplog.text
    up = BackendUpdater(s, None, is_idle=lambda: True, request_restart=lambda: None)
    st = next(b for b in up.state().backends if b.name == "obscura")
    assert st.source == "built-in" and "integrity check failed" in st.error


async def test_obscura_missing_manifest_falls_back(tmp_path, caplog):
    fake_obscura(tmp_path, manifest=False)
    with caplog.at_level(logging.WARNING):
        try:
            found = await obscura._find_obscura_binary(Settings(backends_dir=str(tmp_path)))
        except obscura.BackendUnavailable:
            found = None
    assert found is None or "backends" not in found
    assert "failed its integrity check" in caplog.text


def fake_camoufox(tmp_path):
    root = tmp_path / "camoufox" / "versions" / "v1"
    (root / "camoufox" / "browsers").mkdir(parents=True)
    (root / "camoufox" / "config.json").write_text("{}")
    exe = root / "camoufox" / "browsers" / "camoufox-bin"
    exe.write_bytes(b"x")
    exe.chmod(0o755)
    integrity.write_manifest(root, "v1")
    _switch(tmp_path / "camoufox" / "current", "v1")
    return root


def test_camoufox_overlay_verified_ok(tmp_path):
    fake_camoufox(tmp_path)
    assert camoufox_overlay(Settings(backends_dir=str(tmp_path))) is not None


def test_camoufox_tampered_file_falls_back(tmp_path, caplog):
    root = fake_camoufox(tmp_path)
    (root / "camoufox" / "browsers" / "camoufox-bin").write_bytes(b"y")
    with caplog.at_level(logging.WARNING):
        assert camoufox_overlay(Settings(backends_dir=str(tmp_path))) is None
    assert "failed its integrity check" in caplog.text and "camoufox" in integrity.FAILED


def test_camoufox_extra_executable_falls_back(tmp_path):
    root = fake_camoufox(tmp_path)
    extra = root / "camoufox" / "browsers" / "evil"
    extra.write_bytes(b"z")
    extra.chmod(0o755)
    assert camoufox_overlay(Settings(backends_dir=str(tmp_path))) is None
    assert "unlisted file" in integrity.FAILED["camoufox"]


def test_camoufox_extra_plain_file_falls_back(tmp_path):
    root = fake_camoufox(tmp_path)
    (root / "camoufox" / "browsers" / "user.js").write_text("pref('x', 1);")
    assert camoufox_overlay(Settings(backends_dir=str(tmp_path))) is None
    assert "unlisted file" in integrity.FAILED["camoufox"]


def test_camoufox_planted_symlink_falls_back(tmp_path):
    root = fake_camoufox(tmp_path)
    (root / "camoufox" / "browsers" / "lib.so").symlink_to("/tmp/elsewhere.so")
    assert camoufox_overlay(Settings(backends_dir=str(tmp_path))) is None
    assert "unlisted file" in integrity.FAILED["camoufox"]


def test_manifest_records_and_checks_symlinks(tmp_path):
    (tmp_path / "a").write_text("x")
    (tmp_path / "link").symlink_to("a")
    integrity.write_manifest(tmp_path, "v1")
    assert integrity.verify_manifest(tmp_path, no_extra_exec=True) is None
    (tmp_path / "link").unlink()
    (tmp_path / "link").symlink_to("/etc/passwd")
    assert integrity.verify_manifest(tmp_path, no_extra_exec=True) == "link changed"


def test_camoufox_missing_manifest_falls_back(tmp_path):
    root = fake_camoufox(tmp_path)
    (root / "MANIFEST.json").unlink()
    assert camoufox_overlay(Settings(backends_dir=str(tmp_path))) is None
