import json

import pytest

from mendarr import cli
from mendarr.plugins.store import PluginStore


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MENDARR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("MENDARR_CONFIG_FILE", raising=False)

    def go(*argv):
        capsys.readouterr()
        code = cli.main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    return go


def seed(tmp_path, domain="e.com"):
    s = PluginStore.open(tmp_path / "data")
    try:
        return s.note_failure(domain, kind="challenge_loop", url=f"http://{domain}/", trace_id="", plugin="p")
    finally:
        s.close()


def state(tmp_path, iid):
    s = PluginStore.open(tmp_path / "data")
    try:
        return s.incident(iid)
    finally:
        s.close()


def test_show_retry_close(run, tmp_path):
    iid = seed(tmp_path)
    code, out, _ = run("incidents", "show", str(iid))
    assert code == 0 and json.loads(out)["attempts"] == []
    code, out, _ = run("incidents", "retry", str(iid))
    assert code == 0 and f"incident #{iid} queued" in out
    assert state(tmp_path, iid)["state"] == "open"
    code, out, _ = run("incidents", "close", str(iid))
    assert code == 0 and "solved" in out and state(tmp_path, iid)["state"] == "solved"
    iid2 = seed(tmp_path)
    run("incidents", "close", str(iid2), "--gave-up")
    assert state(tmp_path, iid2)["state"] == "gave_up"


def test_unknown_id(run):
    for action in ("show", "retry", "close"):
        code, _, err = run("incidents", action, "99")
        assert code == 1 and "no incident #99" in err


def test_healer_check_without_config(run):
    code, out, _ = run("healer", "check")
    assert code == 2 and "no LLM profiles configured" in out


def test_notify_test_without_channels(run):
    code, out, _ = run("notify", "test")
    assert code == 2 and "no notification channel configured" in out
